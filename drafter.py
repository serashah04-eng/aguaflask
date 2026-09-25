#!/usr/bin/env python3
"""Skinstinct drafter: Meera's Telegram notes -> triage -> LinkedIn drafts in her voice.

Pipeline:  ingest (Telegram channel)  ->  triage (which notes are worth a post)
           ->  research (one current news/data angle)  ->  draft (voice skill)
           ->  lint (deterministic voice checks)  ->  deliver (drafts/ + Telegram DM)

Nothing is ever posted to LinkedIn. Meera reads, edits and posts herself.

Commands:
  python drafter.py setup-check             verify Telegram bot + show chat ids
  python drafter.py ingest                  pull new notes from the channel
  python drafter.py import-export FILE      backfill from a Telegram Desktop export (result.json)
  python drafter.py run [--posts N]         ingest, triage, research, draft, deliver
  python drafter.py status                  counts of notes by status
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import time
import urllib.request
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

ROOT = Path(__file__).absolute().parent
DATA_FILE = ROOT / "data" / "notes.json"
DRAFTS_DIR = ROOT / "drafts"
VOICE_FILE = ROOT / "voice-skill.txt"
FACTS_FILE = ROOT / "facts.md"

MODEL = "gemini-3.8-flash"  # overridden by GEMINI_MODEL in .env
EXHAUSTED = set()  # models whose daily quota ran out this run
FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"]  # used when the main model is overloaded


# ----------------------------------------------------------------------------
# Config and storage
# ----------------------------------------------------------------------------

def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


def load_store():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"telegram_offset": 0, "notes": {}}


def save_store(store):
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")


def add_note(store, message_id, date, text, source):
    """Notes are keyed by Telegram message id so a backfill and live ingest never duplicate."""
    note_id = f"msg-{message_id}"
    text = (text or "").strip()
    if not text or note_id in store["notes"]:
        return False
    store["notes"][note_id] = {
        "id": note_id,
        "date": date,
        "text": text,
        "source": source,
        "status": "new",  # new | held | discarded | drafted
        "triage_reason": "",
    }
    return True


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

def clean_token(raw):
    """Tolerate common copy-paste mistakes: spaces, line breaks, quotes, or the whole 'NAME=value' line."""
    token = raw.strip().strip('"').strip("'").strip()
    if token.startswith("TELEGRAM_BOT_TOKEN="):
        token = token.split("=", 1)[1].strip().strip('"').strip("'")
    return token


def telegram(method, _timeout=30, **params):
    token = clean_token(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (see .env.example).")
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=_timeout) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload}")
    return payload["result"]


def ingest(store):
    """Pull channel posts. Telegram only keeps updates for ~24h, so run this at least daily."""
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    updates = telegram("getUpdates", offset=store["telegram_offset"],
                       allowed_updates=json.dumps(["channel_post", "edited_channel_post"]))
    added = 0
    for update in updates:
        store["telegram_offset"] = update["update_id"] + 1
        post = update.get("channel_post") or update.get("edited_channel_post")
        if not post:
            continue
        if channel_id and str(post["chat"]["id"]) != channel_id:
            continue
        text = post.get("text") or post.get("caption") or ""
        date = dt.datetime.fromtimestamp(post["date"]).strftime("%Y-%m-%d %H:%M")
        if update.get("edited_channel_post") and f"msg-{post['message_id']}" in store["notes"]:
            store["notes"][f"msg-{post['message_id']}"]["text"] = text.strip()
            continue
        added += add_note(store, post["message_id"], date, text, "telegram")
    save_store(store)
    print(f"Ingested {added} new note(s) from Telegram.")
    return added


def import_export(store, path):
    """Backfill from Telegram Desktop: channel > ... > Export chat history > JSON."""
    export = json.loads(Path(path).read_text(encoding="utf-8"))
    added = 0
    for msg in export.get("messages", []):
        if msg.get("type") != "message":
            continue
        text = msg.get("text", "")
        if isinstance(text, list):  # formatted messages are split into parts
            text = "".join(p if isinstance(p, str) else p.get("text", "") for p in text)
        added += add_note(store, msg["id"], msg.get("date", "")[:16].replace("T", " "), text, "export")
    save_store(store)
    print(f"Imported {added} note(s) from {path}.")


def send_for_review(text):
    chat_id = os.environ.get("TELEGRAM_REVIEW_CHAT_ID")
    if not chat_id:
        print("TELEGRAM_REVIEW_CHAT_ID not set; draft saved to drafts/ only.")
        return
    for start in range(0, len(text), 4000):  # Telegram message limit is 4096 chars
        telegram("sendMessage", chat_id=chat_id, text=text[start:start + 4000],
                 disable_web_page_preview="true")


def setup_check():
    me = telegram("getMe")
    print(f"Bot OK: @{me['username']}")
    updates = telegram("getUpdates")  # no offset: reads without consuming
    seen = {}
    for update in updates:
        for key in ("message", "channel_post"):
            if key in update:
                chat = update[key]["chat"]
                seen[chat["id"]] = f"{chat['type']}: {chat.get('title') or chat.get('first_name', '')}"
    if not seen:
        print("No recent chats. Post something in the channel and send /start to the bot, then rerun.")
    for chat_id, label in seen.items():
        print(f"  {chat_id}  ({label})")
    print("Channel id -> TELEGRAM_CHANNEL_ID. Your private chat id -> TELEGRAM_REVIEW_CHAT_ID.")


# ----------------------------------------------------------------------------
# Gemini calls
# ----------------------------------------------------------------------------

client = None


def llm(system, messages, schema=None, search=False, max_tokens=16000):
    """messages: [{"role": "user"|"assistant", "content": str}]. Returns parsed JSON if schema, else text."""
    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
    )
    if schema:
        config.response_mime_type = "application/json"
        config.response_json_schema = schema
    if search:  # Google Search grounding for the research step
        config.tools = [types.Tool(google_search=types.GoogleSearch())]
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in messages]

    resp = None
    for model in [m for m in [MODEL] + FALLBACK_MODELS if m not in EXHAUSTED][:5] or [MODEL]:
        for attempt in range(2):  # demand spikes pass quickly: one short retry, then the next model
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=config)
                break
            except genai_errors.APIError as e:
                if e.code not in (429, 500, 503, 504):
                    raise
                if "exceeded your current quota" in str(e):
                    print(f"  {model}: quota exhausted for this key; skipping it until restart.")
                    EXHAUSTED.add(model)
                    break  # waiting won't help; go straight to the next model
                print(f"  {model} returned {e.code}.")
            except Exception as e:  # request timeout (client is created with a 90s limit)
                if "timeout" not in type(e).__name__.lower() and "timed out" not in str(e).lower():
                    raise
                print(f"  {model} timed out.")
            if attempt == 0:
                time.sleep(5)
        if resp is not None:
            break
        print(f"  {model} unavailable; trying the next model.")
    if resp is None:
        raise RuntimeError("All Gemini models are busy or rate-limited. Try again later.")

    finish = resp.candidates[0].finish_reason if resp.candidates else None
    if not resp.text:
        raise RuntimeError(f"Gemini returned no text (finish reason: {finish}).")
    if finish and finish.name == "MAX_TOKENS":
        raise RuntimeError("Response hit max_output_tokens before finishing.")
    return json.loads(resp.text) if schema else resp.text


def base_system():
    return (
        "You work in Meera Pillai's founder's office at Skinstinct, an Indian skincare brand. "
        "You turn her rough notes into LinkedIn drafts she will edit and post herself.\n\n"
        "HARD RULES\n"
        "1. Never invent numbers, studies, customer quotes, anecdotes, dates or Skinstinct facts. "
        "Use only what is in her notes, the FACTS file, or a researched source you are given. "
        "Where the post needs something she has not supplied, write a placeholder like "
        "[[MEERA: how many batches failed the mid-run CoA?]].\n"
        "2. Write in her voice as defined in the VOICE SKILL below. It overrides generic "
        "LinkedIn conventions.\n"
        "3. She is not a dermatologist. No medical claims.\n\n"
        f"===== VOICE SKILL =====\n{VOICE_FILE.read_text(encoding='utf-8')}\n\n"
        f"===== FACTS =====\n{FACTS_FILE.read_text(encoding='utf-8')}"
    )


# ----------------------------------------------------------------------------
# Step 1: triage
# ----------------------------------------------------------------------------

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["develop", "hold", "discard"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "verdict", "reason"],
                "additionalProperties": False,
            },
        },
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_ids": {"type": "array", "items": {"type": "string"}},
                    "working_title": {"type": "string"},
                    "core_argument": {"type": "string"},
                    "piece_type": {"type": "string", "enum": [
                        "ingredient_deep_dive", "founder_story", "india_context", "industry_transparency"]},
                    "opening_move": {"type": "string"},
                    "evidence_in_notes": {"type": "string"},
                    "research_query": {"type": "string"},
                    "gaps_for_meera": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["note_ids", "working_title", "core_argument", "piece_type", "opening_move",
                             "evidence_in_notes", "research_query", "gaps_for_meera"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["notes", "posts"],
    "additionalProperties": False,
}

TRIAGE_PROMPT = """Here are Meera's unprocessed notes from her Telegram channel. Decide which are worth developing into a LinkedIn post.

A note is worth developing when it contains at least one of: a specific observation from the manufacturing unit, lab or supplier; a real customer question or pattern; her own data; a gap between what labels/marketing say and what the chemistry or documentation shows; a mistake or decision she can explain. It also needs a point she could argue in her voice without inventing facts.

Verdicts:
- develop: strong enough now (alone or combined with other notes).
- hold: has a seed but needs more from Meera, or would be better combined with a future note.
- discard: logistics, reminders, pure feelings, or something already covered in the published topics list without a new angle.

Then propose up to {n} posts, best first. A post can combine several notes if they make one argument. For each, give the opening move (from the voice skill section 2), what evidence the notes already contain, a web search query for a current news or data angle (India first), and the gaps Meera must fill. Do not propose posts that repeat a published topic unless the notes add something new.

NOTES:
{notes}"""


def triage(notes, n_posts):
    listing = "\n\n".join(f"[{n['id']}] ({n['date']}){' [held earlier]' if n['status'] == 'held' else ''}\n{n['text']}"
                          for n in notes)
    result = llm(base_system(),
                    [{"role": "user", "content": TRIAGE_PROMPT.format(n=n_posts, notes=listing)}],
                    schema=TRIAGE_SCHEMA)
    valid_ids = {n["id"] for n in notes}
    result["posts"] = [p for p in result["posts"] if set(p["note_ids"]) <= valid_ids][:n_posts]
    return result


# ----------------------------------------------------------------------------
# Step 2: research a current angle
# ----------------------------------------------------------------------------

RESEARCH_PROMPT = """Today is {today}. Meera is writing a LinkedIn post with this argument:

{argument}

Find ONE current item she could reference so the post isn't written in a vacuum: a news story, regulatory update (e.g. CDSCO, BIS, ASCI in India), published study, or industry data point, published within roughly the last 90 days. Prefer Indian sources and Indian market relevance. Suggested starting query: {query}

Reply in exactly this format:
HEADLINE:
PUBLISHER:
DATE:
URL:
WHAT IT SAYS: (2-3 factual sentences, no spin)
HOW IT CONNECTS: (1-2 sentences)

If nothing is both recent and genuinely relevant, reply with only: NONE
Do not stretch a weak connection. A post with no news angle is better than a forced one."""


def research(post):
    today = dt.date.today().isoformat()
    try:
        text = llm(
            "You are a careful research assistant. Report only what your sources say.",
            [{"role": "user", "content": RESEARCH_PROMPT.format(
                today=today, argument=post["core_argument"], query=post["research_query"])}],
            search=True,
        ).strip()
    except (RuntimeError, genai_errors.APIError) as e:
        print(f"  research skipped: {e}")
        return "NONE"
    # Keep only the final formatted answer, dropping any narration before it.
    idx = text.find("HEADLINE:")
    if idx >= 0:
        return text[idx:]
    return "NONE"


# ----------------------------------------------------------------------------
# Step 3: draft + lint
# ----------------------------------------------------------------------------

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "post": {"type": "string"},
        "opening_move_used": {"type": "string"},
        "news_reference_used": {"type": "boolean"},
        "placeholders": {"type": "array", "items": {"type": "string"}},
        "self_check": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["item", "passed", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["post", "opening_move_used", "news_reference_used", "placeholders", "self_check"],
    "additionalProperties": False,
}

DRAFT_PROMPT = """Draft a LinkedIn post for Meera.

PLAN FROM TRIAGE
Working title: {title}
Piece type: {piece_type}
Core argument: {argument}
Opening move: {opening}
Evidence already in the notes: {evidence}
Known gaps: {gaps}

HER RAW NOTES (her words; keep her specifics and phrasing where they are good)
{notes}

CURRENT ANGLE FROM RESEARCH
{research}

Instructions:
- Follow the matching structure template in voice skill section 8. LinkedIn: no salutation, no sign-off, no hashtags, no CTA, 350-550 words, prose paragraphs only.
- If the research is NONE, or if using it would feel forced, leave it out. If you use it, name the publisher and date in the sentence the way she would ("A report published by X in August...") and do not add claims beyond WHAT IT SAYS.
- Every fact must trace to the notes, FACTS, or the research. Otherwise use a [[MEERA: ...]] placeholder and list it in placeholders.
- Then run voice skill section 10 (pre-publish checklist) against your draft and report each item in self_check."""


BANNED = [
    "miracle", "revolutionary", "game-changer", "game changer", "holy grail", "glow-up", "flawless",
    "radiant", "skin-loving", "journey", "passion", "passionate", "empower", "hustle", "excited to share",
    "thrilled", "humbled", "toxin-free", "chemical-free", "link in bio", "shop now", "dm me",
    "comment below", "limited time", "we're on a mission",
]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F000-\U0001F2FF]")


def lint(post):
    """Deterministic checks for the rules the voice skill treats as absolute."""
    issues = []
    body = re.sub(r"\[\[MEERA:.*?\]\]", "", post)
    lower = body.lower()
    if "!" in body:
        issues.append("Contains an exclamation mark.")
    if re.search(r"(^|\s)#\w", body):
        issues.append("Contains a hashtag.")
    if EMOJI.search(body):
        issues.append("Contains an emoji.")
    if re.search(r"^\s*([-*•]|\d+[.)])\s", body, re.M):
        issues.append("Contains bullet or numbered list lines; she writes in prose.")
    for word in BANNED:
        if re.search(rf"\b{re.escape(word)}\b", lower):
            issues.append(f"Uses banned phrase: '{word}'.")
    if re.search(r"(?<![\"'“‘])\bclinically (proven|tested)\b", lower):
        issues.append("Uses 'clinically proven/tested' outside quotation marks.")
    for match in re.finditer(r"\b(color|behavior|flavor|favorite|\w*(optimiz|organiz|analyz|recogniz|standardiz|"
                             r"oxidiz|sensitiz|moisturiz|stabiliz|minimiz|maximiz|characteriz)\w*|center(ed)?)\b",
                             lower):
        issues.append(f"American spelling: '{match.group(0)}' (use British/Indian spelling).")
    paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    if len(paragraphs) < 4:
        issues.append(f"Only {len(paragraphs)} paragraph(s); she writes 5-8 short paragraphs separated by blank lines.")
    words = len(body.split())
    if not 300 <= words <= 600:
        issues.append(f"Length is {words} words; target 350-550.")
    return issues


def draft(post, notes, research_text):
    prompt = DRAFT_PROMPT.format(
        title=post["working_title"], piece_type=post["piece_type"], argument=post["core_argument"],
        opening=post["opening_move"], evidence=post["evidence_in_notes"],
        gaps="; ".join(post["gaps_for_meera"]) or "none",
        notes="\n\n".join(f"({n['date']}) {n['text']}" for n in notes),
        research=research_text,
    )
    messages = [{"role": "user", "content": prompt}]
    result = llm(base_system(), messages, schema=DRAFT_SCHEMA)

    issues = lint(result["post"])
    if issues:  # one revision pass against the hard rules
        messages += [
            {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)},
            {"role": "user", "content": "An automated voice check found these problems:\n- "
             + "\n- ".join(issues) + "\nRevise the post to fix them without changing the argument. "
             "Return the full JSON again."},
        ]
        result = llm(base_system(), messages, schema=DRAFT_SCHEMA)
        issues = lint(result["post"])
    result["lint_issues"] = issues
    return result


# ----------------------------------------------------------------------------
# Step 4: deliver
# ----------------------------------------------------------------------------

def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "draft"


def save_draft(post, notes, research_text, result):
    DRAFTS_DIR.mkdir(exist_ok=True)
    today = dt.date.today().isoformat()
    path = DRAFTS_DIR / f"{today}_{slugify(post['working_title'])}.md"
    failed = [c for c in result["self_check"] if not c["passed"]]
    lines = [
        f"# {post['working_title']}",
        f"Drafted {today} | {post['piece_type']} | opening: {result['opening_move_used']}",
        "",
        "## Draft",
        "",
        result["post"],
        "",
        "## Fill in before posting",
        *([f"- {p}" for p in result["placeholders"]] or ["- Nothing: all facts traced to notes, facts.md or research."]),
        "",
        "## Current angle",
        research_text if result["news_reference_used"] else f"Not used.\n\nResearch returned:\n{research_text}",
        "",
        "## Source notes",
        *[f"- [{n['id']}] ({n['date']}) {n['text']}" for n in notes],
        "",
        "## Checks",
        *([f"- LINT: {i}" for i in result["lint_issues"]] or ["- Lint: clean"]),
        *[f"- SELF-CHECK FAILED: {c['item']}: {c['note']}" for c in failed],
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def review_message(post, notes, research_text, result):
    parts = [f"DRAFT: {post['working_title']}", "", result["post"], "", "----"]
    if result["placeholders"]:
        parts.append("Fill in: " + " | ".join(result["placeholders"]))
    if result["news_reference_used"]:
        url = re.search(r"URL:\s*(\S+)", research_text)
        parts.append(f"News angle: {url.group(1) if url else 'see draft file'}")
    parts.append("From notes: " + ", ".join(n["date"] for n in notes))
    if result["lint_issues"]:
        parts.append("Check: " + " ".join(result["lint_issues"]))
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

def run(store, n_posts, send, do_research, do_ingest):
    global client, MODEL
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set (see .env.example).")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=90_000))
    MODEL = os.environ.get("GEMINI_MODEL") or MODEL

    if do_ingest:
        ingest(store)
    pending = [n for n in store["notes"].values() if n["status"] in ("new", "held")]
    if not pending:
        print("No unprocessed notes.")
        return

    print(f"Triaging {len(pending)} note(s)...")
    plan = triage(pending, n_posts)
    by_id = store["notes"]
    for verdict in plan["notes"]:
        if verdict["id"] in by_id:
            by_id[verdict["id"]]["status"] = {"develop": "held", "hold": "held", "discard": "discarded"}[verdict["verdict"]]
            by_id[verdict["id"]]["triage_reason"] = verdict["reason"]
    save_store(store)

    if not plan["posts"]:
        print("Nothing strong enough to draft this run. Notes kept for next time.")
        return

    for post in plan["posts"]:
        notes = [by_id[i] for i in post["note_ids"]]
        print(f"\nPost: {post['working_title']}")
        research_text = research(post) if do_research else "NONE"
        print("  research:", research_text.splitlines()[0])
        result = draft(post, notes, research_text)
        path = save_draft(post, notes, research_text, result)
        print(f"  saved: {path}")
        if send:
            send_for_review(review_message(post, notes, research_text, result))
            print("  sent to Telegram for review")
        for n in notes:
            n["status"] = "drafted"
        save_store(store)


def status(store):
    counts = {}
    for n in store["notes"].values():
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    print(f"{len(store['notes'])} notes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup-check")
    sub.add_parser("ingest")
    sub.add_parser("status")
    imp = sub.add_parser("import-export")
    imp.add_argument("file")
    r = sub.add_parser("run")
    r.add_argument("--posts", type=int, default=1, help="max drafts this run (default 1)")
    r.add_argument("--no-send", action="store_true", help="don't send drafts to Telegram")
    r.add_argument("--no-research", action="store_true", help="skip the web search step")
    r.add_argument("--no-ingest", action="store_true", help="don't pull from Telegram first")
    args = parser.parse_args()

    store = load_store()
    if args.cmd == "setup-check":
        setup_check()
    elif args.cmd == "ingest":
        ingest(store)
    elif args.cmd == "import-export":
        import_export(store, args.file)
    elif args.cmd == "status":
        status(store)
    elif args.cmd == "run":
        run(store, args.posts, not args.no_send, not args.no_research, not args.no_ingest)


if __name__ == "__main__":
    main()
