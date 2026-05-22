"""
Twitter → Telegram Bot  (Render + Redis, multi-channel edition)

Each Telegram channel the bot is added to maintains its own independent list
of tracked Twitter/X accounts. Only original tweets and quote tweets are posted;
pure reposts are silently skipped.
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
#  Twitter helpers
# ─────────────────────────────────────────────

async def fetch_user_tweets(username: str, cfg: Config) -> list[dict]:
    url = "https://twitter-api45.p.rapidapi.com/timeline.php"
    headers = {
        "x-rapidapi-key": cfg.rapidapi_key,
        "x-rapidapi-host": "twitter-api45.p.rapidapi.com",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=headers, params={"screenname": username.lstrip("@")})
        resp.raise_for_status()
        return resp.json().get("timeline", [])


def is_original_or_quote(tweet: dict) -> bool:
    is_retweet = tweet.get("retweeted_status") is not None
    is_quote   = tweet.get("is_quote_status", False) or tweet.get("quoted_status") is not None
    return not (is_retweet and not is_quote)


def tweet_id_of(tweet: dict) -> str:
    return str(tweet.get("tweet_id") or tweet.get("id_str") or tweet.get("id", ""))


def format_message(username: str, tweet: dict) -> str:
    tid     = tweet_id_of(tweet)
    text    = tweet.get("text", "")
    is_qt   = tweet.get("is_quote_status", False) or tweet.get("quoted_status") is not None
    url     = f"https://x.com/{username.lstrip('@')}/status/{tid}"
    label   = "💬 Quote Tweet" if is_qt else "🐦 New Tweet"
    preview = text[:280] + ("…" if len(text) > 280 else "")
    return f"{label} from <b>@{username.lstrip('@')}</b>\n\n{preview}\n\n🔗 {url}"


# ─────────────────────────────────────────────
#  Polling loop
# ─────────────────────────────────────────────

async def poll_channel(bot: Bot, cfg: Config, storage: RedisStorage, channel_id: str):
    accounts = await storage.get_accounts(channel_id)
    for username in accounts:
        try:
            tweets = await fetch_user_tweets(username, cfg)
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
                    text=format_message(username, tweet),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                new_count += 1
            except TelegramError as exc:
                logger.error("Channel %s — Telegram error sending @%s/%s: %s",
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
#  Helpers — resolve the target channel
# ─────────────────────────────────────────────

def get_channel_id(update: Update) -> str | None:
    """
    Commands can be sent in three ways:
      1. Directly in the channel (chat.type == channel)
      2. In a group/supergroup that is linked to a channel
      3. In a private chat with the bot (admin managing the bot)

    For cases 1 & 2 we use the chat ID directly.
    For case 3 the admin must pass the channel ID: /add -1001234 @user
    """
    chat = update.effective_chat
    if chat and chat.type in (Chat.CHANNEL, Chat.GROUP, Chat.SUPERGROUP):
        return str(chat.id)
    return None   # private chat — caller must supply channel_id via argument


def parse_channel_and_users(args: list[str]) -> tuple[str | None, list[str]]:
    """
    Parse optional leading channel ID from command args.
    /add @user1 @user2          → channel=None, users=['user1','user2']
    /add -1001234567890 @user1  → channel='-1001234567890', users=['user1']
    """
    if not args:
        return None, []
    if args[0].lstrip("-").isdigit():
        return args[0], [a.lstrip("@").lower() for a in args[1:]]
    return None, [a.lstrip("@").lower() for a in args]


# ─────────────────────────────────────────────
#  Admin check
# ─────────────────────────────────────────────

def is_admin(user_id: int, cfg: Config) -> bool:
    return not cfg.admin_ids or user_id in cfg.admin_ids


# ─────────────────────────────────────────────
#  Command handlers
# ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config = ctx.bot_data["cfg"]
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id, cfg):
        return

    await update.effective_message.reply_text(
        "👋 <b>Twitter Monitor Bot</b>\n\n"
        "<b>In a channel or group:</b>\n"
        "/add @username — track an account in this channel\n"
        "/remove @username — stop tracking\n"
        "/list — show tracked accounts for this channel\n"
        "/check — check for new tweets now\n"
        "/status — show this channel's stats\n\n"
        "<b>From a private chat (manage any channel):</b>\n"
        "Prefix commands with the channel ID:\n"
        "<code>/add -1001234567890 @username</code>\n"
        "<code>/list -1001234567890</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id, cfg):
        await update.effective_message.reply_text("⛔ Not authorised.")
        return

    channel_from_chat = get_channel_id(update)
    channel_arg, usernames = parse_channel_and_users(ctx.args or [])
    channel_id = channel_arg or channel_from_chat

    if not channel_id:
        await update.effective_message.reply_text(
            "Usage from private chat: /add <channel_id> @username\n"
            "Example: <code>/add -1001234567890 @elonmusk</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if not usernames:
        await update.effective_message.reply_text("Please provide at least one @username.")
        return

    added, already = [], []
    for username in usernames:
        if await storage.add_account(channel_id, username):
            added.append(f"@{username}")
        else:
            already.append(f"@{username}")

    parts = []
    if added:
        parts.append(f"✅ Now tracking in <code>{channel_id}</code>: {', '.join(added)}")
    if already:
        parts.append(f"ℹ️ Already tracked: {', '.join(already)}")
    await update.effective_message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id, cfg):
        await update.effective_message.reply_text("⛔ Not authorised.")
        return

    channel_from_chat = get_channel_id(update)
    channel_arg, usernames = parse_channel_and_users(ctx.args or [])
    channel_id = channel_arg or channel_from_chat

    if not channel_id or not usernames:
        await update.effective_message.reply_text(
            "Usage: /remove @username  (or /remove <channel_id> @username from private chat)"
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
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id, cfg):
        return

    channel_from_chat = get_channel_id(update)
    # /list may be called with an optional channel ID from private chat
    channel_id = None
    if ctx.args and ctx.args[0].lstrip("-").isdigit():
        channel_id = ctx.args[0]
    else:
        channel_id = channel_from_chat

    if not channel_id:
        # Private chat, no channel ID given — list all channels
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
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id, cfg):
        return

    channel_from_chat = get_channel_id(update)
    channel_arg = ctx.args[0] if ctx.args and ctx.args[0].lstrip("-").isdigit() else None
    channel_id  = channel_arg or channel_from_chat

    if channel_id:
        await update.effective_message.reply_text(f"🔍 Checking <code>{channel_id}</code>…", parse_mode=ParseMode.HTML)
        await poll_channel(ctx.bot, cfg, storage, channel_id)
        await update.effective_message.reply_text("✅ Done.")
    else:
        # Private chat, no ID — check all channels
        channels = await storage.get_channels()
        await update.effective_message.reply_text(f"🔍 Checking {len(channels)} channel(s)…")
        for cid in channels:
            await poll_channel(ctx.bot, cfg, storage, cid)
        await update.effective_message.reply_text("✅ All channels checked.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id, cfg):
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
    """Remove all data for a channel (called by admin to clean up)."""
    cfg: Config           = ctx.bot_data["cfg"]
    storage: RedisStorage = ctx.bot_data["storage"]
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id, cfg):
        return

    channel_from_chat = get_channel_id(update)
    channel_arg = ctx.args[0] if ctx.args and ctx.args[0].lstrip("-").isdigit() else None
    channel_id  = channel_arg or channel_from_chat

    if not channel_id:
        await update.effective_message.reply_text(
            "Usage: /leave <channel_id>  — removes all tracking data for that channel."
        )
        return

    removed = await storage.unregister_channel(channel_id)
    if removed:
        await update.effective_message.reply_text(
            f"🗑️ All data for <code>{channel_id}</code> has been removed.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text(
            f"❓ Channel <code>{channel_id}</code> was not registered.",
            parse_mode=ParseMode.HTML,
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

    # Accept commands from channels, groups, and private chats
    any_chat = filters.ALL
    app.add_handler(CommandHandler("start",  cmd_start,  filters=any_chat))
    app.add_handler(CommandHandler("help",   cmd_start,  filters=any_chat))
    app.add_handler(CommandHandler("add",    cmd_add,    filters=any_chat))
    app.add_handler(CommandHandler("remove", cmd_remove, filters=any_chat))
    app.add_handler(CommandHandler("list",   cmd_list,   filters=any_chat))
    app.add_handler(CommandHandler("check",  cmd_check,  filters=any_chat))
    app.add_handler(CommandHandler("status", cmd_status, filters=any_chat))
    app.add_handler(CommandHandler("leave",  cmd_leave,  filters=any_chat))

    logger.info("Bot starting (multi-channel mode)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
