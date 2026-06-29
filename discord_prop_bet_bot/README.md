# Discord Prop Bet Bot

A server-based **prop bet** bot built with [discord.py](https://discordpy.readthedocs.io/). Users create yes/no prediction bets, join by reacting and entering a wager, and compete on a fictional currency leaderboard.

## Features

- Slash commands for creating, resolving, and managing bets
- Per-server balances (every member starts with **1000** coins)
- Reaction-based joining with a wager modal (DM fallback if DMs are closed)
- Automatic bet closing when the time window expires
- Payouts based on YES/NO odds, plus a **refund** outcome for ties / N/A
- SQLite persistence (survives bot restarts)
- Rich embeds for bet messages

## Requirements

- Python 3.11+
- A Discord application / bot token

## Quick start

### 1. Clone and install

```bash
cd discord_prop_bet_bot
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give it a name.
3. Open the **Bot** tab → **Add Bot**.
4. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent** (optional but helpful for mentions)
   - **Message Content Intent** (recommended)
5. Copy the bot **token** (Reset Token if needed).

### 3. Configure `.env`

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # macOS / Linux
```

Edit `.env`:

```env
DISCORD_TOKEN=your_bot_token_here
DATABASE_PATH=propbot.db
STARTING_BALANCE=1000
BET_EXPIRY_CHECK_INTERVAL=30
```

Never commit `.env` or share your token.

### 4. Invite the bot to your server

In the Developer Portal, open **OAuth2 → URL Generator**:

**Scopes**

- `bot`
- `applications.commands`

**Bot permissions** (minimum)

| Permission | Why |
|---|---|
| Send Messages | Post bet embeds |
| Embed Links | Rich embeds |
| Add Reactions | ✅ / ❌ on bet messages |
| Read Message History | Fetch bet messages after restart |
| Use Slash Commands | All `/` commands |

Suggested permission integer: `2147567616` (Send Messages, Embed Links, Add Reactions, Read Message History, Use Application Commands).

Or use **Administrator** for local testing only.

Open the generated URL, pick your server, and authorize.

### 5. Run the bot

```bash
python bot.py
```

You should see `Logged in as ...` and `Slash commands synced` in the console. Slash commands may take up to an hour to appear globally; for faster testing, sync to a single guild in `bot.py`:

```python
await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))
```

## Commands

| Command | Description |
|---|---|
| `/balance` | Your current coin balance |
| `/bet_create` | Create a new prop bet |
| `/bet_resolve` | Resolve a bet (creator or admin) |
| `/bet_cancel` | Cancel and refund an unresolved bet |
| `/bet_status` | Bet details and participants |
| `/my_bets` | Your recent / active bets |
| `/leaderboard` | Top balances in the server |

### Examples

**Create a bet**

```
/bet_create question:"Will Team A win tonight?" duration:2h yes_odds:1.5 no_odds:2.0
```

**Check balance**

```
/balance
```

**Resolve (creator or admin)**

```
/bet_resolve bet_id:1 outcome:YES
/bet_resolve bet_id:1 outcome:NO
/bet_resolve bet_id:1 outcome:Refund (tie / N/A)
```

**Cancel**

```
/bet_cancel bet_id:1
```

**Status & leaderboard**

```
/bet_status bet_id:1
/my_bets
/leaderboard
```

## How betting works

1. Someone runs `/bet_create` — the bot posts an embed with ✅ and ❌ reactions.
2. Users react with their pick. The bot DMs them (or posts a button in-channel) to enter a wager.
3. Wagers are deducted immediately from the user's server balance.
4. Users can change their pick/amount before close; balances are adjusted correctly.
5. Removing a reaction refunds that user's wager.
6. When the duration expires, the bet **closes** — no new wagers.
7. The creator (or a server admin) runs `/bet_resolve`:
   - **Winners** receive `wager × odds`
   - **Losers** keep their wager lost
   - **Refund** returns everyone's wager (tie / N/A)

## Project structure

```
discord_prop_bet_bot/
├── bot.py           # Entry point, background expiry task
├── config.py        # Environment configuration
├── database.py      # SQLite schema and queries
├── models.py        # Dataclasses and enums
├── bets.py          # Duration parsing, payouts, embeds, service logic
├── commands.py      # Slash commands and reaction handlers
├── requirements.txt
├── .env.example
├── pytest.ini
├── README.md
└── tests/
    ├── test_duration.py
    ├── test_payouts.py
    ├── test_database.py
    └── test_bet_service.py
```

## Testing locally

Run the unit test suite (no Discord token required):

```bash
cd discord_prop_bet_bot
pip install -r requirements.txt
pytest -v
```

`pytest` runs with coverage enabled by default (see `pytest.ini` and `tests/conftest.py`). Each test has a **30 second** timeout.

### Coverage policy

| Module | Minimum |
|--------|---------|
| `bets.py`, `database.py` | 90% |
| `models.py` | 100% |
| `channel_policy.py` | 90% |
| `config.py` | 80% |
| `commands.py`, `bot.py` | 70% |
| **Total** | **75%** |

An HTML report is written to `htmlcov/index.html`. See `.cursor/skills/unit-testing/SKILL.md` for testing conventions.

To run tests without the coverage gate:

```bash
pytest -v --no-cov
```

Tests cover duration parsing, payouts, database operations, wagers, refunds, cancellation, and resolution.

### Manual Discord testing checklist

1. Start the bot and confirm slash commands appear.
2. Run `/balance` — should show 1000 coins.
3. Create a short bet (`duration:2m`) and react ✅.
4. Enter a wager via the DM button/modal.
5. Run `/bet_status` and confirm participant list.
6. Wait for expiry or resolve early with `/bet_resolve`.
7. Confirm balances and leaderboard update.
8. Restart the bot with an open bet — it should still close on schedule.

## Edge cases handled

- Duplicate reactions (Discord only fires once per user/emoji)
- Reaction removal refunds wagers
- Bot restart resumes open-bet expiry monitoring
- Expired open bets are closed automatically after restart
- Invalid duration strings return a clear error
- Insufficient funds block wagers
- Only creator/admin can resolve or cancel
- `refund` outcome for ties / not applicable

## License

MIT — use and modify freely.
