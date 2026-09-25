# Skinstinct Telegram bot

Meera sends a note to her Telegram bot. The bot scores it. Notes that score
high enough come back as a LinkedIn draft in her voice, with a current news
angle, for her to APPROVE or REJECT. Nothing is ever published: she posts
approved drafts herself.

Everything happens in Telegram. There's no website, no LinkedIn API and no
scheduling. The bot runs in one of two ways:

- **On Vercel**: Telegram delivers each message to a serverless function
  (`api/webhook.py`), and data is stored in Supabase. It works when your
  computer is off.
- **On this computer**: `python bot.py` polls Telegram, and data is stored in a
  local SQLite file (or in Supabase, if it's configured in `.env`).

```
Meera sends note ─► Vercel webhook (or bot.py polling locally)
                     │
                     ▼
               Gemini scores 0-10 ──── < 6 ──► "Score: 3/10  Reason: ..." (no draft)
                     │ ≥ 6
                     ▼
               Gemini extracts search terms ─► Google News RSS (free, no key)
                     │
                     ▼
               Gemini writes draft (voice-skill.txt + note + facts + news)
               └─ voice lint, one auto-revision if a hard rule is broken
                     │
                     ▼
               "DRAFT LINKEDIN POST … Status: PENDING APPROVAL"
                     │
         Meera replies APPROVE / REJECT ─► status saved (Supabase / SQLite)
```

## Deploy to Vercel

1. **Supabase:** create a free project at supabase.com. Open SQL Editor >
   New query, paste in `supabase_schema.sql` and click Run. Then, under
   Project Settings > API, copy the Project URL and the `service_role` key.
2. **GitHub:** push this folder to a private repository.
3. **Webhook secret:** run `python set_webhook.py --make-secret`. It adds a
   random `TELEGRAM_WEBHOOK_SECRET` to `.env`.
4. **Vercel:** Add New > Project, then import the repository. Before
   deploying, add these environment variables, copying the values from `.env`
   and Supabase: `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `TELEGRAM_WEBHOOK_SECRET`,
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and (optionally)
   `TELEGRAM_ALLOWED_USER_ID`. Then click Deploy.
5. **Connect Telegram:** run `python set_webhook.py https://your-project.vercel.app`.
6. **Check:** `python set_webhook.py --status` shows where Telegram is sending
   messages, and the last error, if there was one.

`vercel.json` gives the function up to 300 seconds, because a slow Gemini day
can take minutes per draft. If Telegram re-sends a message while the bot is
still working on it, the `processed_updates` table makes sure it's only drafted once.

## Run it locally instead

```
pip install google-genai
python bot.py
```

Leave the window open while the bot is running. Stop it with Ctrl+C. If a
Vercel webhook is set, `bot.py` refuses to start, so the two never compete.
`python bot.py --force` removes the webhook and runs locally.

`.env` needs `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY`. `TELEGRAM_ALLOWED_USER_ID`
is optional: if it's empty, the first person to message the bot becomes its
only allowed user.

## Using it (from Telegram)

| Send | What happens |
|---|---|
| any text | Scored. Below 6: the reason, no draft. 6 or above: a draft marked PENDING APPROVAL |
| `APPROVE` | Marks the latest pending draft approved ("ready for you to publish"). Nothing is posted |
| `REJECT` | Marks it rejected. The draft is kept for history |
| `APPROVE` / `REJECT` as a reply to a draft | Decides that specific draft |
| `/pending` | Lists drafts waiting for a decision |
| `/stats` | Totals for notes and drafts |
| `/start`, `/help` | How it works |

## How scoring works

Gemini scores against a strict rubric in `bot.py` (`SCORE_SYSTEM`):

- **0-2:** reminders, logistics, greetings, moods ("Call supplier tomorrow")
- **3-4:** a topic with no point, or an incomplete thought
- **5:** a point, but generic, with nothing specific behind it
- **6-7:** a clear view plus at least one specific: something seen at the unit, a customer pattern, her own data, a label-vs-chemistry gap
- **8-10:** sharp, non-obvious, and concrete enough to draft with few gaps

The pass mark is `PASS_MARK = 6`. Every note is saved with its score and
reason, including rejected ones.

## The news angle

1. Gemini pulls 3-5 search terms and a short search phrase from the note.
2. Google News RSS returns the top 5 stories from the last 30 days (India edition).
3. Gemini decides whether any of them genuinely fits. If none fits, the post has no news reference.
4. If a story is used, the bot appends this block (in code, so it's always there):

```
─────────────────────────────────
NEWS SOURCE: [headline]
FROM: [publication] · [date]
LINK: [url]
⚠ Check this before publishing — you are the author of this claim
─────────────────────────────────
```

The RSS feed carries headlines only, not article text. So the draft may only
claim what the headline states, and the link is there for Meera to check.

## What the drafter must not do

The system prompt forbids inventing experiences, statistics, customer stories,
company facts or quotes. It may use only her note, `facts.md` (things she has
already said publicly) and the news headline. Any detail that's missing becomes
a `[[MEERA: ...]]` placeholder, listed under the draft as "Fill in before
publishing". A deterministic lint (`drafter.lint`) checks her hard voice rules:
no exclamation marks, hashtags, emojis, bullets, hype words or American
spellings, and 350-550 words. If any rule is broken, the draft is revised once.

## Storage

The tables are the same in both backends (`storage.py`): Supabase on Vercel,
and `data/aguaflask.db` (SQLite) locally.

| Table | Contents |
|---|---|
| `notes` | original note, score, score reason, timestamp, Telegram ids |
| `drafts` | original note, draft (with source block), news JSON (search terms, candidates, the one used), status `pending`/`approved`/`rejected`, created and decided timestamps, voice skill version |
| `voice_skill` | the content of `voice-skill.txt`. A new row is added whenever the file changes, and each draft records which version wrote it |
| `settings` | Telegram polling offset, owner user id |
| `processed_updates` | Telegram update ids already handled, so retries are skipped |

To inspect the data: `python -c "import sqlite3; [print(dict(r)) for r in sqlite3.connect('data/aguaflask.db').execute('select id,status,created_at from drafts')]"`

## Files

| File | Purpose |
|---|---|
| `bot.py` | The Telegram bot: scoring, news, drafting, approval loop (and local polling) |
| `api/webhook.py` | Vercel function that receives Telegram messages |
| `storage.py` | SQLite and Supabase storage behind one interface |
| `supabase_schema.sql` | Supabase tables (run once in the SQL editor) |
| `set_webhook.py` | Points Telegram at the Vercel URL, or shows the current setting |
| `vercel.json`, `requirements.txt` | Vercel function settings and Python dependency |
| `drafter.py` | Shared helpers (Telegram API, Gemini calls with retry and model fallback, voice lint), plus the older batch mode |
| `voice-skill.txt` | Meera's voice, built from her 15 published pieces |
| `facts.md` | Facts she has already made public, which the drafter may reuse |
| `.env` | Keys (git-ignored). `.env.example` is the blank template |

## Models

`gemini-3.8-flash` by default (set `GEMINI_MODEL` to change it). If the model is
overloaded or rate-limited, calls automatically fall back to
`gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.5-flash`, then `gemini-3.1-flash-lite`.
Each request has a 90-second time limit.
