#!/usr/bin/env python3
"""Skinstinct Telegram bot: Meera sends a note, gets back a scored, voice-matched LinkedIn draft.

Flow (all inside Telegram, nothing is ever published):
  note -> Gemini score 0-10 -> <6: explain rejection
                            -> >=6: search terms -> Google News RSS -> Gemini draft in Meera's voice
                                    -> draft sent back, PENDING -> Meera replies APPROVE / REJECT -> saved

Run:  python bot.py          (long-polls Telegram; leave it running)
"""

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import drafter  # reuses the existing Telegram helper, Gemini wrapper and voice lint
from drafter import FACTS_FILE, ROOT, VOICE_FILE, genai, lint, llm, telegram

DB_FILE = ROOT / "data" / "aguaflask.db"
PASS_MARK = 6


# ----------------------------------------------------------------------------
# Storage (SQLite: one local file, no external service)
# ----------------------------------------------------------------------------

def db():
    DB_FILE.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER, telegram_message_id INTEGER,
            text TEXT NOT NULL, score INTEGER, score_reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY,
            note_id INTEGER REFERENCES notes(id), note_text TEXT NOT NULL,
            draft TEXT NOT NULL, news_json TEXT, news_used INTEGER NOT NULL DEFAULT 0,
            voice_skill_id INTEGER REFERENCES voice_skill(id),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
            telegram_message_id INTEGER,
            created_at TEXT NOT NULL, decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS voice_skill (
            id INTEGER PRIMARY KEY, content TEXT NOT NULL, sha256 TEXT UNIQUE NOT NULL, loaded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    return conn


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def setting(conn, key, value=None):
    if value is None:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


def current_voice_skill(conn):
    """Store voice-skill.txt; a new row is added only when the file changes, so each draft records which version wrote it."""
    content = VOICE_FILE.read_text(encoding="utf-8")
    digest = hashlib.sha256(content.encode()).hexdigest()
    row = conn.execute("SELECT id FROM voice_skill WHERE sha256 = ?", (digest,)).fetchone()
    if row:
        return row["id"], content
    cur = conn.execute("INSERT INTO voice_skill (content, sha256, loaded_at) VALUES (?, ?, ?)", (content, digest, now()))
    conn.commit()
    return cur.lastrowid, content


# ----------------------------------------------------------------------------
# Step 1: score the note
# ----------------------------------------------------------------------------

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}

SCORE_SYSTEM = """You screen notes for Meera Pillai, founder of Skinstinct (an Indian D2C skincare brand with a pharma formulation background). She drops raw notes to herself; you decide whether a note could become a LinkedIn post in her voice: specific, evidence-led, formulation science and industry transparency.

Score strictly from 0 to 10:
0-2  Not content: reminders, logistics, to-dos, greetings, test messages, pure mood ("tired today"), links with no comment.
3-4  A topic but no point: an incomplete thought, a question with no view, a fragment that would need Meera to supply the entire argument.
5    A point, but generic: anyone could have said it, no specific observation, experience, data or example behind it.
6-7  A clear point of view PLUS at least one specific: something she saw at the unit, a customer question or pattern, her own data, a documented gap between label claims and chemistry, a decision she made and why.
8-10 All of the above, sharp and non-obvious, with enough concrete material that a post could be written with few gaps.

Be strict. Most raw notes are 0-5. Do not reward length, and do not score what the note could become if Meera invented the missing parts. The reason is ONE concise sentence addressed to Meera ("This is a reminder, not an insight." / "Clear point about mid-batch sampling, backed by today's colour shift.")."""


def score_note(text):
    return llm(SCORE_SYSTEM, [{"role": "user", "content": f"NOTE:\n{text}"}], schema=SCORE_SCHEMA, max_tokens=8000)


# ----------------------------------------------------------------------------
# Step 2: news angle (Gemini search terms -> Google News RSS, no key needed)
# ----------------------------------------------------------------------------

TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {"type": "array", "items": {"type": "string"}},
        "search_phrase": {"type": "string"},
    },
    "required": ["terms", "search_phrase"],
    "additionalProperties": False,
}


def search_terms(text):
    return llm(
        "Extract search terms for finding current news related to a skincare founder's note.",
        [{"role": "user", "content":
          "Give 3-5 search terms from this note, then one short Google News search phrase (2-5 words) "
          "most likely to find a current, relevant news story. Prefer industry, ingredient, regulatory or "
          "Indian market terms over generic words. Do not include brand names.\n\nNOTE:\n" + text}],
        schema=TERMS_SCHEMA, max_tokens=8000)


def google_news(phrase, limit=5):
    """Top recent Google News results. Summary is the headline itself: RSS carries no article text."""
    query = urllib.parse.quote(f"({phrase}) (skincare OR cosmetics OR beauty OR dermatology) when:30d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())
    items = []
    for item in list(root.iter("item"))[:limit]:
        source = item.find("source")
        publication = source.text if source is not None else ""
        headline = item.findtext("title", "")
        if publication and headline.endswith(f" - {publication}"):
            headline = headline[: -len(f" - {publication}")]
        pub = item.findtext("pubDate", "")
        try:
            pub = dt.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z").strftime("%d %b %Y")
        except ValueError:
            pass
        items.append({"headline": headline, "publication": publication, "date": pub, "url": item.findtext("link", "")})
    return items


RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "best_index": {"type": "integer"},
        "relevant": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["best_index", "relevant", "reason"],
    "additionalProperties": False,
}

RELEVANCE_PROMPT = """Meera's note:
{note}

News headlines found:
{news}

Pick the single best headline and decide if it is GENUINELY relevant. Relevant means BOTH:
1. It is about skincare, cosmetics, personal care, ingredients, formulation, manufacturing quality, or the regulation, testing or marketing of these products (India preferred).
2. A thoughtful reader would see a direct link to the specific point in her note, not just a shared word.
A story from another industry, a funding announcement, a market-size or market-research report, a press-release style explainer, or a keyword coincidence is NOT relevant. When in doubt, relevant = false."""


def find_news(text):
    """Returns (search terms, all candidates, the one relevant item or None)."""
    terms = search_terms(text)
    candidates, seen = [], set()
    for phrase in [terms["search_phrase"], " ".join(terms["terms"][:2])]:
        try:
            for item in google_news(phrase):
                if item["url"] not in seen:
                    seen.add(item["url"])
                    candidates.append(item)
        except Exception as e:
            print(f"  news search failed: {e}")
    if not candidates:
        return terms, [], None
    news = "\n".join(f"[{i}] {n['headline']} | {n['publication']} | {n['date']}" for i, n in enumerate(candidates))
    verdict = llm("You judge news relevance strictly.",
                  [{"role": "user", "content": RELEVANCE_PROMPT.format(note=text, news=news)}],
                  schema=RELEVANCE_SCHEMA, max_tokens=8000)
    print(f"  news relevance: {verdict['relevant']} ({verdict['reason']})")
    chosen = candidates[verdict["best_index"]] if verdict["relevant"] and 0 <= verdict["best_index"] < len(candidates) else None
    return terms, candidates, chosen


# ----------------------------------------------------------------------------
# Step 3: draft in Meera's voice
# ----------------------------------------------------------------------------

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "post": {"type": "string"},
        "news_index": {"type": "integer"},  # index of the news item used, or -1 for none
        "news_summary": {"type": "string"},  # one line, only what the headline states; "" if none used
        "placeholders": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["post", "news_index", "news_summary", "placeholders"],
    "additionalProperties": False,
}


def draft_system(voice):
    return (
        "You write LinkedIn post drafts for Meera Pillai, founder of Skinstinct. She will edit and publish them herself.\n\n"
        "The VOICE SKILL below is your writing instruction. It describes exactly how she writes; follow it over any "
        "generic LinkedIn habit.\n\n"
        "DO NOT INVENT anything: no personal experiences, statistics, customer stories, company facts, quotes, dates, or "
        "claims that are not in her note, the FACTS list, or the news headline you are given. Where the post genuinely "
        "needs a detail she hasn't supplied, write a placeholder like [[MEERA: how many batches were affected?]] and "
        "list it in placeholders. Keep her original insight and point of view; do not change her argument.\n\n"
        f"===== VOICE SKILL (voice-skill.txt) =====\n{voice}\n\n"
        f"===== FACTS SHE HAS ALREADY MADE PUBLIC =====\n{FACTS_FILE.read_text(encoding='utf-8')}"
    )


DRAFT_PROMPT = """Write a LinkedIn post from Meera's note.

MEERA'S ORIGINAL NOTE (her insight; keep her point and her specifics):
{note}

CURRENT NEWS FOUND FOR THIS NOTE (headline, publication, date only; no article text):
{news}

LinkedIn instructions:
- 350-550 words in 5-8 short prose paragraphs separated by blank lines, no headers, bullets, hashtags, emojis, exclamation marks or call to action.
- Open the way the voice skill describes (section 2), follow its structure templates (section 8) and rules (section 9).
- News: decide if one item is GENUINELY relevant to her point. If so, reference it naturally in one or two sentences, naming the publication, and claim nothing beyond what its headline states; set news_index to its number and write a one-line news_summary restating the headline. If none fits naturally, ignore them all: news_index = -1 and news_summary = "". Never force it.
- Do not add a source block or links; that is appended separately.
- Stay inside what the note says happened. Do NOT add when it happened (no "last week", "yesterday") unless the note says so, who she spoke to, what anyone replied beyond the note, earlier experiences ("I have seen this before"), other industries, costs, or outcomes. General formulation science that explains her point is fine; new events are not. If the argument needs a fact she hasn't given, use a [[MEERA: ...]] placeholder."""


def news_block(item):
    return ("\n─────────────────────────────────\n"
            f"NEWS SOURCE: {item['headline']}\n"
            f"FROM: {item['publication']} · {item['date']}\n"
            f"LINK: {item['url']}\n"
            "⚠ Check this before publishing — you are the author of this claim\n"
            "─────────────────────────────────")


def write_draft(note, voice, news_items):
    news = "\n".join(f"[{i}] {n['headline']} | {n['publication']} | {n['date']}" for i, n in enumerate(news_items)) \
        or "None found."
    messages = [{"role": "user", "content": DRAFT_PROMPT.format(note=note, news=news)}]
    system = draft_system(voice)
    result = llm(system, messages, schema=DRAFT_SCHEMA)

    issues = lint(result["post"])
    if issues:  # one revision pass against her hard voice rules
        messages += [
            {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)},
            {"role": "user", "content": "An automated voice check found:\n- " + "\n- ".join(issues)
             + "\nFix these without changing the argument. Return the full JSON again."},
        ]
        result = llm(system, messages, schema=DRAFT_SCHEMA)
    if not 0 <= result["news_index"] < len(news_items):
        result["news_index"] = -1
    return result


# ----------------------------------------------------------------------------
# Telegram handling
# ----------------------------------------------------------------------------

HELP = ("Send me any note: an observation from the unit, a customer DM, something you read.\n\n"
        "I score it 0-10. Below 6 I tell you why and stop. At 6 or above I find a current news angle, "
        "write a LinkedIn draft in your voice, and send it back for you to APPROVE or REJECT.\n\n"
        "Nothing is ever published. You post it yourself.\n\n"
        "/pending  drafts waiting for a decision\n/stats  totals")


def send(chat_id, text, reply_to=None):
    last = None
    for start in range(0, len(text), 4000):  # Telegram's limit is 4096 characters
        params = {"chat_id": chat_id, "text": text[start:start + 4000], "disable_web_page_preview": "true"}
        if reply_to and start == 0:
            params["reply_to_message_id"] = reply_to
            params["allow_sending_without_reply"] = "true"
        last = telegram("sendMessage", **params)
    return last["message_id"]


def handle_note(conn, chat_id, message_id, text):
    telegram("sendChatAction", chat_id=chat_id, action="typing")
    result = score_note(text)
    score, reason = max(0, min(10, int(result["score"]))), result["reason"].strip()
    note_id = conn.execute(
        "INSERT INTO notes (chat_id, telegram_message_id, text, score, score_reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, message_id, text, score, reason, now())).lastrowid
    conn.commit()
    print(f"  note #{note_id} scored {score}/10: {reason}")

    if score < PASS_MARK:
        send(chat_id, f"Score: {score}/10\nReason: {reason}\n\nNot strong enough for a post, so no draft. "
                      "It's saved; add the specific (what you saw, the number, the customer question) and send it again.",
             reply_to=message_id)
        return

    send(chat_id, f"Score: {score}/10\nReason: {reason}\n\nFinding a news angle and drafting...", reply_to=message_id)
    telegram("sendChatAction", chat_id=chat_id, action="typing")
    voice_id, voice = current_voice_skill(conn)
    terms, candidates, chosen = find_news(text)
    news_items = [chosen] if chosen else []  # the drafter only ever sees news that passed the relevance check
    print(f"  search: {terms['search_phrase']!r} -> {len(candidates)} result(s), relevant: {bool(chosen)}")
    telegram("sendChatAction", chat_id=chat_id, action="typing")
    result = write_draft(text, voice, news_items)

    used = news_items[result["news_index"]] if result["news_index"] >= 0 else None
    if used:
        used = {**used, "summary": result["news_summary"]}
    body = result["post"].strip() + (("\n" + news_block(used)) if used else "")
    news_record = {"search_terms": terms, "candidates": candidates, "passed_relevance": chosen, "used": used}
    draft_id = conn.execute(
        "INSERT INTO drafts (note_id, note_text, draft, news_json, news_used, voice_skill_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (note_id, text, body, json.dumps(news_record, ensure_ascii=False), int(bool(used)), voice_id, now())).lastrowid
    conn.commit()

    extra = ""
    if result["placeholders"]:
        extra = "\n\nFill in before publishing:\n" + "\n".join(f"• {p}" for p in result["placeholders"])
    msg_id = send(chat_id, f"DRAFT LINKEDIN POST  (#{draft_id})\n\n{body}{extra}\n\n"
                           "Status: PENDING APPROVAL\n"
                           "Reply APPROVE or REJECT. To decide on an older draft, reply to that draft's message.")
    conn.execute("UPDATE drafts SET telegram_message_id = ? WHERE id = ?", (msg_id, draft_id))
    conn.commit()
    print(f"  draft #{draft_id} sent (news used: {bool(used)})")


def handle_decision(conn, chat_id, decision, reply_to_msg_id):
    """APPROVE / REJECT applies to the draft being replied to, else the most recent pending draft."""
    row = None
    if reply_to_msg_id:
        row = conn.execute("SELECT * FROM drafts WHERE telegram_message_id = ?", (reply_to_msg_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM drafts WHERE status = 'pending' ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        send(chat_id, "There's no pending draft to decide on.")
        return
    status = "approved" if decision == "APPROVE" else "rejected"
    if row["status"] == status:
        print(f"  draft #{row['id']} already {status}")
        send(chat_id, f"Draft #{row['id']} is already {status}.", reply_to=row["telegram_message_id"])
        return
    if row["status"] != "pending" and not reply_to_msg_id:
        # A bare APPROVE/REJECT only ever decides pending drafts; changing a decision needs an explicit reply.
        send(chat_id, "There's no pending draft. To change your decision on a draft, reply to that draft's message.")
        return
    previous = row["status"]
    conn.execute("UPDATE drafts SET status = ?, decided_at = ? WHERE id = ?", (status, now(), row["id"]))
    conn.commit()
    print(f"  draft #{row['id']} {previous} -> {status}")
    if status == "approved":
        send(chat_id, f"Approved. This draft is ready for you to publish. (#{row['id']})",
             reply_to=row["telegram_message_id"])
    else:
        send(chat_id, f"Rejected. Draft #{row['id']} is marked rejected and kept in history so we can learn from it.",
             reply_to=row["telegram_message_id"])


def handle_update(conn, update):
    msg = update.get("message")
    if not msg or msg["chat"]["type"] != "private":
        return
    chat_id, user_id = msg["chat"]["id"], msg["from"]["id"]

    # Only Meera may use the bot: the first person to message it becomes the owner
    # (or set TELEGRAM_ALLOWED_USER_ID in .env).
    owner = os.environ.get("TELEGRAM_ALLOWED_USER_ID") or setting(conn, "owner_user_id")
    if owner is None:
        setting(conn, "owner_user_id", user_id)
        owner = str(user_id)
        print(f"  owner set to user {user_id}")
    if str(user_id) != str(owner):
        send(chat_id, "This is a private bot.")
        return

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        send(chat_id, "I can only read text notes for now. Please type the note, or paste a transcription.")
        return
    print(f"[{now()}] message {msg['message_id']}: {text[:70]!r}")

    command = text.split()[0].split("@")[0].lower()
    if command in ("/start", "/help"):
        send(chat_id, HELP)
    elif command == "/stats":
        n = conn.execute("SELECT COUNT(*), SUM(score >= ?) FROM notes", (PASS_MARK,)).fetchone()
        d = dict(conn.execute("SELECT status, COUNT(*) FROM drafts GROUP BY status").fetchall())
        send(chat_id, f"Notes: {n[0]} ({n[1] or 0} scored {PASS_MARK}+)\n"
                      f"Drafts: {d.get('pending', 0)} pending, {d.get('approved', 0)} approved, {d.get('rejected', 0)} rejected")
    elif command == "/pending":
        rows = conn.execute("SELECT id, note_text, created_at FROM drafts WHERE status = 'pending' ORDER BY id").fetchall()
        send(chat_id, "\n".join(f"#{r['id']} ({r['created_at'][:10]}): {r['note_text'][:60]}" for r in rows)
             or "No pending drafts.")
    elif re.fullmatch(r"(approve|reject)[.!]?", text, re.I):
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        handle_decision(conn, chat_id, text.upper().rstrip(".!"), reply_to)
    elif command.startswith("/"):
        send(chat_id, "Unknown command. " + HELP)
    else:
        handle_note(conn, chat_id, msg["message_id"], text)


def main():
    drafter.load_env()
    for key in ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"):
        if not os.environ.get(key):
            sys.exit(f"{key} is not set in .env")
    drafter.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                                  http_options=drafter.types.HttpOptions(timeout=90_000))  # ms
    drafter.MODEL = os.environ.get("GEMINI_MODEL") or drafter.MODEL

    conn = db()
    current_voice_skill(conn)
    if telegram("getWebhookInfo").get("url"):  # polling can't run while a webhook is set
        telegram("deleteWebhook")
        print("Removed an old webhook so the bot can poll.")
    me = telegram("getMe")
    print(f"@{me['username']} is running with {drafter.MODEL}. Ctrl+C to stop.")

    offset = int(setting(conn, "telegram_offset") or 0)
    while True:
        try:
            updates = telegram("getUpdates", _timeout=60, offset=offset, timeout=50,
                               allowed_updates=json.dumps(["message"]))
        except Exception as e:
            print(f"Telegram poll failed ({e}); retrying in 5s")
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            setting(conn, "telegram_offset", offset)  # saved before handling, so a crash never replays a note
            try:
                handle_update(conn, update)
            except Exception as e:
                traceback.print_exc()
                chat = (update.get("message") or {}).get("chat", {}).get("id")
                if chat:
                    try:
                        send(chat, f"Something went wrong processing that ({type(e).__name__}: {str(e)[:200]}). "
                                   "Your note was not lost if it was saved; please try again in a minute.")
                    except Exception:
                        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
