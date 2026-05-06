# Updated Discord points bot with 3 separate leaderboards:
# - Warzone
# - Multiplayer
# - MoD Ranked
#
# Notes:
# - Black Ops Royale remains mapped to Warzone at 2 points for historical fairness.
# - /mypoints, /leaderboard, and /export now require/select a leaderboard.
# - /addpoints and /removepoints now require a leaderboard selection.
# - Manual adjustments now store leaderboard_key.
# - Existing score_events rows get leaderboard_key backfilled on startup when possible.

import csv
import io
import os
import random
import sqlite3
import time
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
BOT_ROLE_ID = int(os.getenv("BOT_ROLE_ID", "708012967308034138"))
ACK_COOLDOWN_SECONDS = int(os.getenv("ACK_COOLDOWN_SECONDS", "10"))
ACK_USE_REACTION_FALLBACK = os.getenv("ACK_USE_REACTION_FALLBACK", "true").lower() == "true"

PRANK_ENABLED = os.getenv("PRANK_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
PRANK_CHANNEL_ID = int(os.getenv("PRANK_CHANNEL_ID", "0"))
PRANK_EVERY_N_SCORE_POSTS = int(os.getenv("PRANK_EVERY_N_SCORE_POSTS", "3"))

PRANK_QUOTES = [
    "Milk of Duty really scraping the bottom of the milk carton with your gameplay, {name}.",
    "That lobby must’ve been sponsored by Fisher-Price if you survived that, {name}.",
    "You move through Warzone like a divorced dad looking for parking, {name}.",
    "Milk of Duty HR wants to know how you still have a roster spot, {name}.",
    "That aim assist worked harder than you did, {name}.",
    "You spent the whole match looting just to donate gear to the first real player you saw, {name}.",
    "You play like your controller batteries are dying in real time, {name}.",
    "I’ve seen stronger movement from NPC civilians, {name}.",
    "Milk of Duty might need to lactose-free your contract after that performance, {name}.",
    "Your gun skill looks AI generated, {name}.",
    "You rotate into zone like it personally offended you, {name}.",
    "The only thing getting carried harder than your backpack is your K/D, {name}.",
    "I’ve seen supermarket cashiers with better reaction time, {name}.",
    "You camp so hard the game should charge you rent, {name}.",
    "Milk of Duty doctors officially diagnosed that gameplay as terminally fraudulent, {name}.",
    "You panic reload so often I’m surprised your reload button still works, {name}.",
    "The Gulag staff know you by first name at this point, {name}.",
    "That win was less skill and more a statistical accident, {name}.",
    "You third-party fights like a raccoon digging through leftovers, {name}.",
    "Your teammates deserve combat pay for babysitting you, {name}.",
    "You loot every building like you’re filing taxes in there, {name}.",
    "Milk of Duty command confirms your backpack contributed more than your aim, {name}.",
    "Watching your gameplay lowers squad morale, {name}.",
    "You move around the map like your operator owes child support, {name}.",
    "Your callouts sound like someone reading IKEA instructions under pressure, {name}.",
    "That loadout is doing all the work while you sightsee, {name}.",
    "Even the bots spectating were embarrassed for you, {name}.",
    "You spent the whole game hiding just to lose the final gunfight anyway, {name}.",
    "Milk of Duty is considering replacing you with a Roomba after that performance, {name}.",
    "Honestly {name}, that gameplay should qualify as a war crime against aim.",
]

_prank_score_post_counter: dict[int, int] = {}



async def maybe_send_score_prank(message: discord.Message) -> None:
    if not PRANK_ENABLED:
        return
    if not message.guild:
        return
    if message.author.bot:
        return
    if not PRANK_CHANNEL_ID:
        return
    if message.channel.id != PRANK_CHANNEL_ID:
        return
    if PRANK_EVERY_N_SCORE_POSTS <= 0:
        return

    guild_id = message.guild.id
    current_count = _prank_score_post_counter.get(guild_id, 0) + 1
    _prank_score_post_counter[guild_id] = current_count

    if current_count % PRANK_EVERY_N_SCORE_POSTS != 0:
        return

    quote = random.choice(PRANK_QUOTES).format(name=message.author.mention)

    try:
        await message.channel.send(quote)
    except discord.HTTPException as exc:
        print(f"Failed to send score prank message: {exc}")


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
    category_tag, base_points, _role_id, board_key = category
    multiplier, _bonus_name = bonus_multiplier_for_guild(message.guild.id)
    points = int(base_points * multiplier)

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
            leaderboard_key=board_key,
        )

    mark_processed(message.id, message.guild.id, message.channel.id)
    await maybe_send_score_prank(message)
    return True


def has_mod_access(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    allowed_role_ids = {role_id for role_id in (MOD_ROLE_ID, BOT_ROLE_ID) if role_id}
    if allowed_role_ids and any(role.id in allowed_role_ids for role in member.roles):
        return True
    return False


def resolve_member_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"User {user_id}"


async def build_leaderboard_embed(guild: discord.Guild, period_label: str, start_at: Optional[str], end_at: Optional[str], leaderboard_key: str) -> discord.Embed:
    rows = query_top_users(guild.id, start_at, end_at, leaderboard_key=leaderboard_key, limit=20)
    embed = discord.Embed(title=f"{BOARD_LABELS.get(leaderboard_key, leaderboard_key)} - {period_label}")
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


def build_summary_csv(guild: discord.Guild, period_label: str, start_at: Optional[str], end_at: Optional[str], leaderboard_key: str) -> bytes:
    rows = query_top_users(guild.id, start_at, end_at, leaderboard_key=leaderboard_key, limit=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["rank", "player_name", "user_id", "period", "leaderboard", "total_points"])
    for idx, row in enumerate(rows, start=1):
        user_id = int(row["awarded_user_id"])
        writer.writerow([idx, resolve_member_name(guild, user_id), user_id, period_label, BOARD_LABELS.get(leaderboard_key, leaderboard_key), int(row["total"])])
    return output.getvalue().encode("utf-8")


def build_breakdown_csv(guild: discord.Guild, period_label: str, start_at: Optional[str], end_at: Optional[str], leaderboard_key: str) -> bytes:
    rows = query_breakdown_rows(guild.id, start_at, end_at, leaderboard_key=leaderboard_key)
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
    writer.writerow(["player_name", "user_id", "leaderboard", *sorted_categories, "total_points", "period"])
    for user_id in sorted(totals_by_user.keys(), key=lambda uid: resolve_member_name(guild, uid).lower()):
        row_totals = totals_by_user[user_id]
        values = [row_totals.get(category, 0) for category in sorted_categories]
        writer.writerow([resolve_member_name(guild, user_id), user_id, BOARD_LABELS.get(leaderboard_key, leaderboard_key), *values, sum(values), period_label])
    return output.getvalue().encode("utf-8")


def build_events_csv(guild: discord.Guild, period_label: str, start_at: Optional[str], end_at: Optional[str], leaderboard_key: str) -> bytes:
    rows = query_event_rows(guild.id, start_at, end_at, leaderboard_key=leaderboard_key)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["created_at_utc", "message_id", "source_channel_id", "poster_name", "poster_user_id", "awarded_name", "awarded_user_id", "category_tag", "leaderboard", "points", "period"])
    for row in rows:
        poster_id = int(row["poster_user_id"])
        awarded_id = int(row["awarded_user_id"])
        writer.writerow([
            row["created_at"], row["message_id"], row["source_channel_id"],
            resolve_member_name(guild, poster_id), poster_id,
            resolve_member_name(guild, awarded_id), awarded_id,
            row["category_tag"], BOARD_LABELS.get(leaderboard_key, leaderboard_key),
            row["points"], period_label
        ])
    return output.getvalue().encode("utf-8")


@bot.event
async def on_ready():
    ensure_bonus_events_table()
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
            category_tag, base_points, role_id, board_key = extract_matching_category(message)  # type: ignore[misc]
            multiplier, _bonus_name = bonus_multiplier_for_guild(message.guild.id)
            points = int(base_points * multiplier)
            recipients = get_awarded_members(message)
            await send_score_ack(message, category_tag, points, role_id, recipients, board_key)
        except Exception as exc:
            print(f"Failed to send scoring acknowledgement for message {message.id}: {exc}")
    await bot.process_commands(message)


BOARD_CHOICES = [
    app_commands.Choice(name="Warzone", value="warzone"),
    app_commands.Choice(name="Multiplayer", value="multiplayer"),
    app_commands.Choice(name="MoD Ranked", value="mod_ranked"),
]
EXPORT_TYPE_CHOICES = [
    app_commands.Choice(name="summary", value="summary"),
    app_commands.Choice(name="breakdown", value="breakdown"),
    app_commands.Choice(name="events", value="events"),
]


@bot.tree.command(name="mypoints", description="Show your points for one leaderboard.")
@app_commands.describe(
    board="Leaderboard to check",
    player="Optional player to check",
    season="Optional season name. Defaults to active season if one exists",
    start_date="Optional start date in YYYY-MM-DD format",
    end_date="Optional end date in YYYY-MM-DD format",
)
@app_commands.choices(board=BOARD_CHOICES)
async def mypoints(interaction: discord.Interaction, board: app_commands.Choice[str], player: Optional[discord.Member] = None, season: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None):
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    try:
        period_label, start_at, end_at = resolve_period(interaction.guild.id, season_name=season, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    target = player or interaction.user
    total = query_total_points(interaction.guild.id, target.id, start_at, end_at, board.value)
    embed = discord.Embed(title=BOARD_LABELS.get(board.value, board.value), description=f"**{target.display_name}** has **{total}** point(s) for **{period_label}**.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Show one leaderboard.")
@app_commands.describe(
    board="Leaderboard to show",
    season="Optional season name. Defaults to active season if one exists",
    start_date="Optional start date in YYYY-MM-DD format",
    end_date="Optional end date in YYYY-MM-DD format",
)
@app_commands.choices(board=BOARD_CHOICES)
async def leaderboard(interaction: discord.Interaction, board: app_commands.Choice[str], season: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message("You need administrator permission or the configured mod role to use this command.", ephemeral=True)
        return
    try:
        period_label, start_at, end_at = resolve_period(interaction.guild.id, season_name=season, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    embed = await build_leaderboard_embed(interaction.guild, period_label, start_at, end_at, board.value)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="addpoints", description="Add points to a user for one leaderboard.")
@app_commands.describe(player="Player to receive points", board="Leaderboard that should receive the points", value="How many points to add", reason="Reason label for the manual adjustment", month="Optional month bucket in YYYY-MM format for bookkeeping")
@app_commands.choices(board=BOARD_CHOICES)
async def addpoints(interaction: discord.Interaction, player: discord.Member, board: app_commands.Choice[str], value: int, reason: Optional[str] = None, month: Optional[str] = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message("You need administrator permission or the configured mod role to use this command.", ephemeral=True)
        return
    if value <= 0:
        await interaction.response.send_message("Value must be greater than 0.", ephemeral=True)
        return
    try:
        month_key = normalize_month_key(month)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    record_manual_adjustment(interaction.guild.id, player.id, interaction.user.id, value, reason or "manual_add", month_key, board.value)
    period_label, start_at, end_at = resolve_period(interaction.guild.id)
    new_total = query_total_points(interaction.guild.id, player.id, start_at, end_at, board.value)
    await interaction.response.send_message(f"Added **{value}** point(s) to **{player.display_name}** in **{BOARD_LABELS.get(board.value, board.value)}**. Current total for **{period_label}**: **{new_total}**.", ephemeral=True)


@bot.tree.command(name="removepoints", description="Remove points from a user for one leaderboard.")
@app_commands.describe(player="Player to remove points from", board="Leaderboard that should lose the points", value="How many points to remove", reason="Reason label for the manual adjustment", month="Optional month bucket in YYYY-MM format for bookkeeping")
@app_commands.choices(board=BOARD_CHOICES)
async def removepoints(interaction: discord.Interaction, player: discord.Member, board: app_commands.Choice[str], value: int, reason: Optional[str] = None, month: Optional[str] = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message("You need administrator permission or the configured mod role to use this command.", ephemeral=True)
        return
    if value <= 0:
        await interaction.response.send_message("Value must be greater than 0.", ephemeral=True)
        return
    try:
        month_key = normalize_month_key(month)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    record_manual_adjustment(interaction.guild.id, player.id, interaction.user.id, -value, reason or "manual_remove", month_key, board.value)
    period_label, start_at, end_at = resolve_period(interaction.guild.id)
    new_total = query_total_points(interaction.guild.id, player.id, start_at, end_at, board.value)
    await interaction.response.send_message(f"Removed **{value}** point(s) from **{player.display_name}** in **{BOARD_LABELS.get(board.value, board.value)}**. Current total for **{period_label}**: **{new_total}**.", ephemeral=True)


@bot.tree.command(name="export", description="Export one leaderboard.")
@app_commands.describe(export_type="summary, breakdown, or events", board="Leaderboard to export", season="Optional season name. Defaults to active season if one exists", start_date="Optional start date in YYYY-MM-DD format", end_date="Optional end date in YYYY-MM-DD format")
@app_commands.choices(export_type=EXPORT_TYPE_CHOICES, board=BOARD_CHOICES)
async def export_data(interaction: discord.Interaction, export_type: app_commands.Choice[str], board: app_commands.Choice[str], season: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message("You need administrator permission or the configured mod role to use this command.", ephemeral=True)
        return
    try:
        period_label, start_at, end_at = resolve_period(interaction.guild.id, season_name=season, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    if export_type.value == "summary":
        data = build_summary_csv(interaction.guild, period_label, start_at, end_at, board.value)
        prefix = "leaderboard_summary"
    elif export_type.value == "breakdown":
        data = build_breakdown_csv(interaction.guild, period_label, start_at, end_at, board.value)
        prefix = "leaderboard_breakdown"
    else:
        data = build_events_csv(interaction.guild, period_label, start_at, end_at, board.value)
        prefix = "leaderboard_events"

    safe_period = period_label.replace(":", "").replace(" ", "_")
    filename = f"{prefix}_{board.value}_{safe_period}.csv"
    await interaction.response.send_message(content=f"Export ready for **{BOARD_LABELS.get(board.value, board.value)}** / **{period_label}** ({export_type.value}).", file=discord.File(io.BytesIO(data), filename=filename), ephemeral=True)


@bot.tree.command(name="seasoncreate", description="Create or update a season.")
@app_commands.describe(name="Season name", start_date="Start date in YYYY-MM-DD format", end_date="Optional end date in YYYY-MM-DD format")
async def seasoncreate(interaction: discord.Interaction, name: str, start_date: str, end_date: Optional[str] = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message("You need administrator permission or the configured mod role to use this command.", ephemeral=True)
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
    await interaction.response.send_message(f"Saved season **{name}** with start **{start_date}**" + (f" and end **{end_date}**." if end_date else "."), ephemeral=True)


@bot.tree.command(name="seasonsetactive", description="Set the active season.")
@app_commands.describe(name="Season name")
async def seasonsetactive(interaction: discord.Interaction, name: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message("You need administrator permission or the configured mod role to use this command.", ephemeral=True)
        return
    if not set_active_season(interaction.guild.id, name):
        await interaction.response.send_message(f"Season '{name}' was not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"Set **{name}** as the active season.", ephemeral=True)


@bot.tree.command(name="seasonclose", description="Close a season and optionally set its end date.")
@app_commands.describe(name="Season name", end_date="Optional end date in YYYY-MM-DD format")
async def seasonclose(interaction: discord.Interaction, name: str, end_date: Optional[str] = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message("You need administrator permission or the configured mod role to use this command.", ephemeral=True)
        return
    try:
        end_at = normalize_date_input(end_date, end_of_day=True) if end_date else None
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    if not close_season(interaction.guild.id, name, end_at=end_at):
        await interaction.response.send_message(f"Season '{name}' was not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"Closed season **{name}**" + (f" with end date **{end_date}**." if end_date else "."), ephemeral=True)


@bot.tree.command(name="seasonlist", description="List configured seasons.")
async def seasonlist(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message("You need administrator permission or the configured mod role to use this command.", ephemeral=True)
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
    await interaction.response.send_message(embed=discord.Embed(title="Configured Seasons", description="\n".join(lines)), ephemeral=True)



@bot.tree.command(name="bonusevent_start", description="Start a bonus points event.")
@app_commands.describe(
    name="Bonus event name, e.g. Double Points Weekend",
    multiplier="Points multiplier, e.g. 2 for double points",
    start_date="Start date in YYYY-MM-DD format",
    end_date="End date in YYYY-MM-DD format",
)
async def bonusevent_start(
    interaction: discord.Interaction,
    name: str,
    multiplier: float,
    start_date: str,
    end_date: str,
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
    if multiplier <= 0:
        await interaction.response.send_message("Multiplier must be greater than 0.", ephemeral=True)
        return

    try:
        start_at = normalize_date_input(start_date, end_of_day=False)
        end_at = normalize_date_input(end_date, end_of_day=True)
        if start_at > end_at:
            raise ValueError("start_date must be on or before end_date.")
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    create_bonus_event(interaction.guild.id, name, multiplier, start_at, end_at)
    await interaction.response.send_message(
        f"Started bonus event **{name}**: **{multiplier}x** from **{start_date}** through **{end_date}**.",
        ephemeral=True,
    )


@bot.tree.command(name="bonusevent_end", description="End the active bonus points event.")
async def bonusevent_end(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    if not end_active_bonus_event(interaction.guild.id):
        await interaction.response.send_message("There is no active bonus event to end.", ephemeral=True)
        return

    await interaction.response.send_message("Ended the active bonus event.", ephemeral=True)


@bot.tree.command(name="bonusevent_status", description="Show the current bonus points event status.")
async def bonusevent_status(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    active = get_active_bonus_event(interaction.guild.id)
    if active:
        start_str = utc_iso_to_display(str(active["start_at"]))
        end_str = utc_iso_to_display(str(active["end_at"]))
        await interaction.response.send_message(
            f"Active bonus event: **{active['name']}** — **{active['multiplier']}x** from **{start_str}** through **{end_str}**.",
            ephemeral=True,
        )
        return

    latest = get_latest_bonus_event(interaction.guild.id)
    if latest and int(latest["is_active"]) == 1:
        start_str = utc_iso_to_display(str(latest["start_at"]))
        end_str = utc_iso_to_display(str(latest["end_at"]))
        await interaction.response.send_message(
            f"A bonus event is configured but not currently active: **{latest['name']}** — **{latest['multiplier']}x** from **{start_str}** through **{end_str}**.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("No active bonus event.", ephemeral=True)


@tasks.loop(minutes=1)
async def leaderboard_task():
    now = datetime.now(ZoneInfo(BOT_TIMEZONE))
    schedule_map = {}
    for hour in POST_HOURS:
        schedule_map[(hour, 0)] = "warzone"
        schedule_map[(hour, 5)] = "multiplayer"
        schedule_map[(hour, 10)] = "mod_ranked"

    board_key = schedule_map.get((now.hour, now.minute))
    if board_key is None:
        return

    for guild in bot.guilds:
        channel = guild.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel is None:
            continue
        try:
            period_label, start_at, end_at = resolve_period(guild.id)
            embed = await build_leaderboard_embed(guild, period_label, start_at, end_at, board_key)
            await channel.send(embed=embed)
        except Exception as exc:
            print(f"Failed to post leaderboard {board_key} for guild {guild.id}: {exc}")


@leaderboard_task.before_loop
async def before_leaderboard_task():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable")
    if SOURCE_CHANNEL_ID == 0:
        raise RuntimeError("Missing SOURCE_CHANNEL_ID environment variable")
    if LEADERBOARD_CHANNEL_ID == 0:
        raise RuntimeError("Missing LEADERBOARD_CHANNEL_ID environment variable")
    bot.run(DISCORD_TOKEN)
