"""
Twitter -> Telegram Bot  (Render + Redis, multi-channel edition)

Each Telegram channel the bot is added to maintains its own independent list
of tracked Twitter/X accounts. Only original tweets and quote tweets are posted;
pure reposts are silently skipped.

API: twitter-api47 on RapidAPI
  - Resolve username -> userId once on /add, cached in Redis
  - Poll tweets using userId + optional cursor
"""

import asyncio
import logging

import httpx
from telegram import Bot, Chat, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters
from telegram.error import TelegramError

from config import Config
from storage import RedisStorage

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Twitter API helpers
# ─────────────────────────────────────────────

def _api_headers(cfg: Config) -> dict:
    return {
        "x-rapidapi-key":  cfg.rapidapi_key,
        "x-rapidapi-host": cfg.rapidapi_host,
    }


async def resolve_user_id(username: str, cfg: Config) -> str | None:
    """
    Look up the Twitter user ID for a given username.
    Returns the ID string, or None if the user wasn't found.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            cfg.rapidapi_profile_url,
            headers=_api_headers(cfg),
            params={"username": username.lstrip("@")},
        )
        resp.raise_for_status()
        data = resp.json()

    # Response shape may vary — handle common structures
    # Shape A: { "id": "...", "username": "..." }
    # Shape B: { "data": { "id": "...", "username": "..." } }
    if isinstance(data, dict):
        user = data.get("data", data)
        uid = user.get("id") or user.get("userId") or user.get("rest_id")
        if uid:
            return str(uid)

    logger.warning("Could not extract user ID from profile response for @%s: %s", username, data)
    return None


async def fetch_user_tweets(user_id: str, cfg: Config) -> list[dict]:
    """
    Fetch recent tweets for a user by their Twitter user ID.
    Response shape: { "data": [ { "id", "type", "text", "author": {...} } ] }
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            cfg.rapidapi_tweets_url,
            headers=_api_headers(cfg),
            params={"userId": user_id},
        )
        resp.raise_for_status()
        data = resp.json()

    return data.get("data", [])


def is_original_or_quote(tweet: dict) -> bool:
    """type is one of: "tweet", "quote", "retweet". Skip pure retweets."""
    return tweet.get("type") != "retweet"


def tweet_id_of(tweet: dict) -> str:
    return str(tweet.get("id", ""))


def format_message(tweet: dict) -> str:
    tid      = tweet_id_of(tweet)
    text     = tweet.get("text", "")
    ttype    = tweet.get("type", "tweet")
    username = tweet.get("author", {}).get("username", "unknown")
    url      = f"https://x.com/{username}/status/{tid}"
    label    = "💬 Quote Tweet" if ttype == "quote" else "🐦 New Tweet"
    preview  = text[:280] + ("…" if len(text) > 280 else "")
    return (
        f"{label} from <b>@{username}</b>\n\n"
        f"{preview}\n\n"
        f"🔗 {url}"
    )


# ─────────────────────────────────────────────
#  Polling loop
# ─────────────────────────────────────────────

async def poll_channel(bot: Bot, cfg: Config, storage: RedisStorage, channel_id: str):
    accounts = await storage.get_accounts(channel_id)
    for username in accounts:
        # Resolve user ID (from cache or fresh lookup)
        user_id = await storage.get_user_id(username)
        if not user_id:
            logger.info("No cached user ID for @%s, resolving...", username)
            user_id = await resolve_user_id(username, cfg)
            if not user_id:
                logger.warning("Could not resolve user ID for @%s, skipping.", username)
                continue
            await storage.set_user_id(username, user_id)

        try:
            tweets = await fetch_user_tweets(user_id, cfg)
        except Exception as exc:
            logger.warning("Channel %s — failed to fetch @%s: %s", channel_id, username, exc)
            continue

        new_count = 0
        for tweet in tweets:
            tid = tweet_id_of(tweet)
            if not tid:
                continue
            if await storage.is_seen(channel_id, username, tid):
                continue
            if not is_original_or_quote(tweet):
                await storage.mark_seen(channel_id, username, tid)
                continue

            try:
                await bot.send_message(
                    chat_id=channel_id,
                    text=format_message(tweet),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                new_count += 1
            except TelegramError as exc:
                logger.error("Channel %s — Telegram error @%s/%s: %s",
                             channel_id, username, tid, exc)

            await storage.mark_seen(channel_id, username, tid)
            await asyncio.sleep(0.3)

        if new_count:
            logger.info("Channel %s @%s → %d new tweet(s)", channel_id, username, new_count)


async def polling_loop(bot: Bot, cfg: Config, storage: RedisStorage):
    logger.info("Polling loop started (interval: %ds)", cfg.poll_interval)
    while True:
        try:
            channels = await storage.get_channels()
            for channel_id in channels:
                await poll_channel(bot, cfg, storage, channel_id)
        except Exception as exc:
            logger.exception("Unexpected error in polling loop: %s", exc)
        await asyncio.sleep(cfg.poll_interval)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def get_channel_id(update: Update) -> str | None:
    chat = update.effective_chat
    if chat and chat.type in (Chat.CHANNEL, Chat.GROUP, Chat.SUPERGROUP):
        return str(chat.id)
    return None


def parse_channel_and_users(args: list[str]) -> tuple[str | None, list[str]]:
    if not args:
        return None, []
    if args[0].lstrip("-").isdigit():
        return args[0], [a.lstrip("@").lower() for a in args[1:]]
    return None, [a.lstrip("@").lower() for a in args]


def is_admin(user_id: int, cfg: Config) -> bool:
    return not cfg.admin_ids or user_id in cfg.admin_ids


# ─────────────────────────────────────────────
#  Command handlers
# ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config = ctx.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        return
    await update.effective_message.reply_text(
        "👋 <b>Twitter Monitor Bot</b>\n\n"
        "<b>In a channel or group:</b>\n"
        "/add @username — track an account in this channel\n"
        "/remove @username — stop tracking\n"
        "/list — show tracked accounts\n"
        "/check — check for new tweets now\n"
        "/status — show stats\n\n"
        "<b>From private chat (manage any channel):</b>\n"
        "<code>/add -1001234567890 @username</code>\n"
        "<code>/list -1001234567890</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    if not is_admin(update.effective_user.id, cfg):
        await update.effective_message.reply_text("⛔ Not authorised.")
        return

    channel_from_chat = get_channel_id(update)
    channel_arg, usernames = parse_channel_and_users(ctx.args or [])
    channel_id = channel_arg or channel_from_chat

    if not channel_id:
        await update.effective_message.reply_text(
            "Usage from private chat:\n<code>/add -1001234567890 @username</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if not usernames:
        await update.effective_message.reply_text("Please provide at least one @username.")
        return

    await update.effective_message.reply_text("🔍 Resolving account(s)…")

    added, already, failed = [], [], []
    for username in usernames:
        # Check if already tracked in this channel
        existing = await storage.get_accounts(channel_id)
        if username in existing:
            already.append(f"@{username}")
            continue

        # Resolve user ID (use cache if available from another channel)
        user_id = await storage.get_user_id(username)
        if not user_id:
            try:
                user_id = await resolve_user_id(username, cfg)
            except Exception as exc:
                logger.warning("Failed to resolve @%s: %s", username, exc)

        if not user_id:
            failed.append(f"@{username}")
            continue

        await storage.set_user_id(username, user_id)
        await storage.add_account(channel_id, username)
        added.append(f"@{username}")

    parts = []
    if added:
        parts.append(f"✅ Now tracking in <code>{channel_id}</code>: {', '.join(added)}")
    if already:
        parts.append(f"ℹ️ Already tracked: {', '.join(already)}")
    if failed:
        parts.append(f"❌ Could not find: {', '.join(failed)} — check the username and try again")
    await update.effective_message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    if not is_admin(update.effective_user.id, cfg):
        await update.effective_message.reply_text("⛔ Not authorised.")
        return

    channel_from_chat = get_channel_id(update)
    channel_arg, usernames = parse_channel_and_users(ctx.args or [])
    channel_id = channel_arg or channel_from_chat

    if not channel_id or not usernames:
        await update.effective_message.reply_text(
            "Usage: /remove @username  (or /remove <channel_id> @username)"
        )
        return

    removed, not_found = [], []
    for username in usernames:
        if await storage.remove_account(channel_id, username):
            removed.append(f"@{username}")
        else:
            not_found.append(f"@{username}")

    parts = []
    if removed:
        parts.append(f"🗑️ Removed from <code>{channel_id}</code>: {', '.join(removed)}")
    if not_found:
        parts.append(f"❓ Not tracked: {', '.join(not_found)}")
    await update.effective_message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    if not is_admin(update.effective_user.id, cfg):
        return

    channel_from_chat = get_channel_id(update)
    channel_id = None
    if ctx.args and ctx.args[0].lstrip("-").isdigit():
        channel_id = ctx.args[0]
    else:
        channel_id = channel_from_chat

    if not channel_id:
        # Private chat, no ID — list all channels
        channels = await storage.get_channels()
        if not channels:
            await update.effective_message.reply_text("No channels registered yet.")
            return
        lines = []
        for cid in sorted(channels):
            accounts = await storage.get_accounts(cid)
            accs = ", ".join(f"@{a}" for a in accounts) or "(none)"
            lines.append(f"<code>{cid}</code>: {accs}")
        await update.effective_message.reply_text(
            f"📋 <b>All channels ({len(channels)}):</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
        return

    accounts = await storage.get_accounts(channel_id)
    if not accounts:
        await update.effective_message.reply_text(
            f"No accounts tracked in <code>{channel_id}</code>. Use /add @username.",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = "\n".join(f"• @{a}" for a in accounts)
    await update.effective_message.reply_text(
        f"📋 <b>Tracked in <code>{channel_id}</code> ({len(accounts)}):</b>\n{lines}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    if not is_admin(update.effective_user.id, cfg):
        return

    channel_from_chat = get_channel_id(update)
    channel_arg = ctx.args[0] if ctx.args and ctx.args[0].lstrip("-").isdigit() else None
    channel_id  = channel_arg or channel_from_chat

    if channel_id:
        await update.effective_message.reply_text(
            f"🔍 Checking <code>{channel_id}</code>…", parse_mode=ParseMode.HTML
        )
        await poll_channel(ctx.bot, cfg, storage, channel_id)
        await update.effective_message.reply_text("✅ Done.")
    else:
        channels = await storage.get_channels()
        await update.effective_message.reply_text(f"🔍 Checking {len(channels)} channel(s)…")
        for cid in channels:
            await poll_channel(ctx.bot, cfg, storage, cid)
        await update.effective_message.reply_text("✅ All channels checked.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    if not is_admin(update.effective_user.id, cfg):
        return

    channel_from_chat = get_channel_id(update)
    channel_arg = ctx.args[0] if ctx.args and ctx.args[0].lstrip("-").isdigit() else None
    channel_id  = channel_arg or channel_from_chat

    if channel_id:
        stats = await storage.channel_stats(channel_id)
        await update.effective_message.reply_text(
            f"⚙️ <b>Channel <code>{channel_id}</code></b>\n\n"
            f"Tracked accounts: {stats['accounts']}\n"
            f"Tweets seen: {stats['seen']}\n"
            f"Poll interval: {cfg.poll_interval}s",
            parse_mode=ParseMode.HTML,
        )
    else:
        stats = await storage.global_stats()
        await update.effective_message.reply_text(
            f"⚙️ <b>Global Status</b>\n\n"
            f"Channels: {stats['channels']}\n"
            f"Total tracked accounts: {stats['accounts']}\n"
            f"Total tweets seen: {stats['seen']}\n"
            f"Poll interval: {cfg.poll_interval}s",
            parse_mode=ParseMode.HTML,
        )


async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    if not is_admin(update.effective_user.id, cfg):
        return

    channel_from_chat = get_channel_id(update)
    channel_arg = ctx.args[0] if ctx.args and ctx.args[0].lstrip("-").isdigit() else None
    channel_id  = channel_arg or channel_from_chat

    if not channel_id:
        await update.effective_message.reply_text(
            "Usage: /leave <channel_id>"
        )
        return

    removed = await storage.unregister_channel(channel_id)
    if removed:
        await update.effective_message.reply_text(
            f"🗑️ All data for <code>{channel_id}</code> removed.", parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(
            f"❓ Channel <code>{channel_id}</code> was not registered.", parse_mode=ParseMode.HTML
        )


# ─────────────────────────────────────────────
#  Startup
# ─────────────────────────────────────────────

async def post_init(application: Application) -> None:
    cfg     = application.bot_data["cfg"]
    storage = application.bot_data["storage"]
    application.create_task(polling_loop(application.bot, cfg, storage))


def main():
    cfg     = Config()
    storage = RedisStorage(cfg.redis_url)

    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(post_init)
        .build()
    )

    app.bot_data["cfg"]     = cfg
    app.bot_data["storage"] = storage

    app.add_handler(CommandHandler("start",  cmd_start,  filters=filters.ALL))
    app.add_handler(CommandHandler("help",   cmd_start,  filters=filters.ALL))
    app.add_handler(CommandHandler("add",    cmd_add,    filters=filters.ALL))
    app.add_handler(CommandHandler("remove", cmd_remove, filters=filters.ALL))
    app.add_handler(CommandHandler("list",   cmd_list,   filters=filters.ALL))
    app.add_handler(CommandHandler("check",  cmd_check,  filters=filters.ALL))
    app.add_handler(CommandHandler("status", cmd_status, filters=filters.ALL))
    app.add_handler(CommandHandler("leave",  cmd_leave,  filters=filters.ALL))

    logger.info("Bot starting (multi-channel mode)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
