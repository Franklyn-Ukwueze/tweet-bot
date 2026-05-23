# Twitter → Telegram Monitor Bot — Fly.io Edition

One bot, any number of Telegram channels. Each channel has its own independent list of tracked Twitter/X accounts. Original tweets and quote tweets only — reposts skipped.

---

## Deploy to Fly.io (free tier)

### Prerequisites

Install the Fly CLI:
```bash
# macOS
brew install flyctl

# Linux
curl -L https://fly.io/install.sh | sh

# Windows
pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"
```

Then log in:
```bash
fly auth login
```

---

### Step 1 — Get your API keys

**Telegram bot:**
1. Message **@BotFather** → `/newbot` → copy the token
2. Add the bot as **Administrator** to every channel you want to monitor (needs "Post Messages" permission)

**RapidAPI:**
1. Sign up at https://rapidapi.com
2. Search **"Tweet API"** by **twitter-api47** → subscribe to the free plan
3. Copy your RapidAPI key

**Your Telegram user ID** (to restrict bot commands to yourself):
- Message **@userinfobot** on Telegram → copy the number

---

### Step 2 — Create the Fly app

```bash
# From inside the project folder:
fly launch --no-deploy
```

When prompted:
- **App name:** choose a unique name (this goes in fly.toml too — update it)
- **Region:** pick your nearest one
- **Postgres / Redis:** say **No** to both (we add Redis separately below)

Then edit `fly.toml` and replace `"your-app-name"` with the name you just chose.

---

### Step 3 — Add Redis (Upstash, free tier)

```bash
fly ext upstash redis create
```

- Choose a name for the Redis instance
- Select the same region as your app
- Fly automatically sets `REDIS_URL` as a secret in your app — no manual steps needed

---

### Step 4 — Set secrets

```bash
fly secrets set \
  TELEGRAM_BOT_TOKEN="123456789:ABCdef..." \
  RAPIDAPI_KEY="your_rapidapi_key_here" \
  ADMIN_USER_IDS="your_telegram_user_id"
```

---

### Step 5 — Deploy

```bash
fly deploy
```

Check it's running:
```bash
fly logs        # live log stream
fly status      # machine status
```

---

## Managing the bot

### Start tracking accounts

Message your bot privately or run commands inside a channel:

```
/add @naval @sama @paulg
```

### From a private chat (manage any channel remotely)

```
/add -1001234567890 @naval     ← prefix with numeric channel ID
/list                          ← lists ALL channels and their accounts
/status                        ← global stats
/leave -1001234567890          ← remove all data for a channel
```

Get a channel's numeric ID by forwarding any message from it to **@userinfobot**.

---

## Bot Commands

| Command | Description |
|---|---|
| `/add @username` | Track a Twitter account in this channel |
| `/remove @username` | Stop tracking |
| `/list` | Show tracked accounts |
| `/check` | Force an immediate poll now |
| `/status` | Show stats |
| `/leave` | Remove all tracking data for a channel |

---

## Useful Fly.io commands

```bash
fly logs                  # stream live logs
fly status                # check machine health
fly secrets list          # list set secrets (values hidden)
fly secrets set KEY=val   # add/update a secret
fly deploy                # redeploy after code changes
fly scale count 1         # ensure exactly 1 instance is running
```

---

## Free tier limits

- **Fly.io:** 3 shared-CPU VMs free, 256 MB RAM each — more than enough for this bot
- **Upstash Redis:** 10,000 commands/day free, 256 MB storage
- **RapidAPI:** 500 requests/month on the free plan

With the default 5-minute poll interval and a handful of accounts, you'll stay well within all limits.

---

## Project structure

```
twitterbot/
├── bot.py            # Polling loop + Telegram command handlers
├── config.py         # Env var loader
├── storage.py        # Async Redis storage (multi-channel)
├── requirements.txt
├── Dockerfile        # Container build for Fly.io
├── fly.toml          # Fly.io app config
├── .env.example      # Local dev reference
├── .gitignore
└── README.md
```
