# Twitter → Telegram Monitor Bot — Multi-Channel Edition

One bot instance, any number of Telegram channels. Each channel has its own independent list of tracked Twitter/X accounts. Only original tweets and quote tweets are forwarded — pure reposts are silently skipped.

---

## How It Works

- Add the bot as Admin to as many channels as you like
- Run `/add @username` **inside each channel** to track accounts per-channel
- The bot polls Twitter every 5 minutes and posts new tweets only to the relevant channel
- All state is stored in Redis — survives redeployments

---

## Setup

### 1 — Create a Telegram Bot

1. Message **@BotFather** → `/newbot` → copy the **token**
2. Add the bot as **Administrator** to every channel you want to monitor (needs "Post Messages" permission)
3. **Important:** in BotFather, run `/setprivacy` → select your bot → choose **Disable** so the bot can read commands sent in groups/channels

### 2 — Get a RapidAPI Key

1. Sign up free at https://rapidapi.com
2. Search **"Twitter API v2"** by **twitter-api45** → subscribe to the free plan
3. Copy your **RapidAPI Key**

### 3 — Deploy to Render via Blueprint

1. Push this folder to a new GitHub repo
2. Render dashboard → **New → Blueprint** → connect the repo
3. Render reads `render.yaml` and provisions the bot worker + Redis together
4. Fill in the environment variables (Step 4) → **Apply**

### 4 — Environment Variables

Set these in the Blueprint setup screen or Render → your service → **Environment**:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | From @BotFather |
| `RAPIDAPI_KEY` | ✅ | From rapidapi.com |
| `ADMIN_USER_IDS` | recommended | Comma-separated Telegram user IDs who can use commands |
| `POLL_INTERVAL_SECONDS` | ❌ | Default `300` (5 min). Already set in render.yaml |
| `REDIS_URL` | auto | Injected automatically — do not set manually |

> **No `TELEGRAM_CHANNEL_ID` needed** — the bot is multi-channel and discovers channels automatically when you run commands inside them.

---

## Usage

### From inside a channel or group
Just run commands directly — the bot knows which channel it's in:

```
/add @naval @sama          → track these accounts in this channel
/remove @naval             → stop tracking in this channel
/list                      → show accounts tracked in this channel
/check                     → force an immediate tweet check for this channel
/status                    → show stats for this channel
```

### From a private chat with the bot
Prefix commands with the channel ID to manage any channel remotely:

```
/add -1001234567890 @naval
/remove -1001234567890 @naval
/list -1001234567890
/check -1001234567890
/status -1001234567890
/list                      → (no ID) lists ALL channels and their accounts
/status                    → (no ID) shows global stats across all channels
/leave -1001234567890      → removes all tracking data for that channel
```

> Get a channel's numeric ID by forwarding a message from it to **@userinfobot**.

---

## Bot Commands Reference

| Command | Description |
|---|---|
| `/add [@username...]` | Track one or more Twitter accounts in this channel |
| `/remove [@username...]` | Stop tracking accounts in this channel |
| `/list` | Show tracked accounts (all channels if sent privately) |
| `/check` | Trigger an immediate poll right now |
| `/status` | Show stats (global if sent privately) |
| `/leave` | Remove all tracking data for a channel (cleanup) |

---

## What Gets Posted

| Tweet type | Forwarded? |
|---|---|
| Original tweet | ✅ Yes |
| Quote tweet | ✅ Yes |
| Retweet / Repost | ❌ No |

---

## Redis Key Schema

```
tw:channels                        — SET of all channel IDs
tw:accounts:<channel_id>           — SET of tracked usernames per channel
tw:seen:<channel_id>:<username>    — SET of already-posted tweet IDs
```

---

## Project Structure

```
twitterbot/
├── bot.py            # Polling loop + multi-channel command handlers
├── config.py         # Env var loader
├── storage.py        # Async Redis storage — multi-channel schema
├── requirements.txt
├── render.yaml       # Render Blueprint (worker + Redis)
├── runtime.txt       # Python 3.11
├── .env.example
├── .gitignore
└── README.md
```

---

## Troubleshooting

**Bot doesn't receive commands in a channel**
→ Make sure Privacy Mode is disabled: BotFather → `/setprivacy` → your bot → Disable.

**Bot posts to the wrong channel / doesn't post at all**
→ Confirm the bot is Admin with "Post Messages" in that channel. Run `/list` inside the channel to verify accounts are tracked there.

**`REDIS_URL` not found on startup**
→ You deployed as a plain Web Service instead of a Blueprint. Use **New → Blueprint** so Redis is provisioned and linked automatically.

**Free tier limits**
→ Render: 750 worker hours/month (enough for one always-on service), Redis: 25 MB cap.
→ RapidAPI free tier: 500 req/month. With multiple channels × multiple accounts, increase `POLL_INTERVAL_SECONDS` or upgrade to the $10/mo RapidAPI tier.
