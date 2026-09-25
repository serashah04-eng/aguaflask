"""Storage for the bot: notes, drafts, voice skill versions, settings.

Two backends with the same methods:
  SQLiteStore    local file (data/aguaflask.db), used when running bot.py on your computer
  SupabaseStore  Supabase Postgres over its REST API, used on Vercel (files don't persist there)

get_store() picks Supabase when SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set.
"""

import datetime as dt
import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).absolute().parent


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


class SQLiteStore:
    def __init__(self, path=ROOT / "data" / "aguaflask.db"):
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
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
            CREATE TABLE IF NOT EXISTS processed_updates (update_id INTEGER PRIMARY KEY, received_at TEXT NOT NULL);
        """)

    def _one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def claim_update(self, update_id):
        """True the first time an update is seen; False for Telegram retries of the same update."""
        try:
            self.conn.execute("INSERT INTO processed_updates VALUES (?, ?)", (update_id, now()))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_setting(self, key):
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    def voice_skill_id(self, content):
        digest = sha256(content)
        row = self._one("SELECT id FROM voice_skill WHERE sha256 = ?", (digest,))
        if row:
            return row["id"]
        cur = self.conn.execute("INSERT INTO voice_skill (content, sha256, loaded_at) VALUES (?, ?, ?)",
                                (content, digest, now()))
        self.conn.commit()
        return cur.lastrowid

    def add_note(self, chat_id, message_id, text, score, reason):
        cur = self.conn.execute(
            "INSERT INTO notes (chat_id, telegram_message_id, text, score, score_reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, text, score, reason, now()))
        self.conn.commit()
        return cur.lastrowid

    def add_draft(self, note_id, note_text, draft, news, news_used, voice_skill_id):
        cur = self.conn.execute(
            "INSERT INTO drafts (note_id, note_text, draft, news_json, news_used, voice_skill_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (note_id, note_text, draft, json.dumps(news, ensure_ascii=False), int(news_used), voice_skill_id, now()))
        self.conn.commit()
        return cur.lastrowid

    def set_draft_message(self, draft_id, message_id):
        self.conn.execute("UPDATE drafts SET telegram_message_id = ? WHERE id = ?", (message_id, draft_id))
        self.conn.commit()

    def draft_by_message(self, message_id):
        return self._one("SELECT * FROM drafts WHERE telegram_message_id = ?", (message_id,))

    def latest_pending(self):
        return self._one("SELECT * FROM drafts WHERE status = 'pending' ORDER BY id DESC LIMIT 1")

    def decide(self, draft_id, status):
        self.conn.execute("UPDATE drafts SET status = ?, decided_at = ? WHERE id = ?", (status, now(), draft_id))
        self.conn.commit()

    def note_scores(self):
        return [r["score"] for r in self.conn.execute("SELECT score FROM notes")]

    def draft_statuses(self):
        return [r["status"] for r in self.conn.execute("SELECT status FROM drafts")]

    def pending(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT id, note_text, created_at FROM drafts WHERE status = 'pending' ORDER BY id")]


class SupabaseStore:
    """Same interface, backed by Supabase's REST API (PostgREST). Tables come from supabase_schema.sql."""

    def __init__(self, url, service_key):
        url = url.strip().rstrip("/")
        if url.endswith("/rest/v1"):  # accept the "API URL" form as well as the plain project URL
            url = url[: -len("/rest/v1")]
        self.base = url + "/rest/v1/"
        self.headers = {"apikey": service_key, "Content-Type": "application/json"}
        if service_key.startswith("eyJ"):  # legacy service_role JWT; new sb_secret_ keys go in apikey only
            self.headers["Authorization"] = f"Bearer {service_key}"

    def _req(self, method, table, query=None, body=None, prefer=None):
        url = self.base + table + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def _insert(self, table, row):
        return self._req("POST", table, body=row, prefer="return=representation")[0]

    def _first(self, table, query):
        rows = self._req("GET", table, {**query, "limit": "1"})
        return rows[0] if rows else None

    def claim_update(self, update_id):
        try:
            self._req("POST", "processed_updates", body={"update_id": update_id}, prefer="return=minimal")
            return True
        except urllib.error.HTTPError as e:
            if e.code == 409:  # primary key already exists: Telegram retried this update
                return False
            raise

    def get_setting(self, key):
        row = self._first("settings", {"key": f"eq.{key}", "select": "value"})
        return row["value"] if row else None

    def set_setting(self, key, value):
        self._req("POST", "settings", body={"key": key, "value": str(value)},
                  prefer="resolution=merge-duplicates,return=minimal")

    def voice_skill_id(self, content):
        digest = sha256(content)
        row = self._first("voice_skill", {"sha256": f"eq.{digest}", "select": "id"})
        if row:
            return row["id"]
        return self._insert("voice_skill", {"content": content, "sha256": digest})["id"]

    def add_note(self, chat_id, message_id, text, score, reason):
        return self._insert("notes", {"chat_id": chat_id, "telegram_message_id": message_id, "text": text,
                                      "score": score, "score_reason": reason})["id"]

    def add_draft(self, note_id, note_text, draft, news, news_used, voice_skill_id):
        return self._insert("drafts", {"note_id": note_id, "note_text": note_text, "draft": draft,
                                       "news_json": news, "news_used": bool(news_used),
                                       "voice_skill_id": voice_skill_id, "status": "pending"})["id"]

    def set_draft_message(self, draft_id, message_id):
        self._req("PATCH", "drafts", {"id": f"eq.{draft_id}"}, {"telegram_message_id": message_id}, "return=minimal")

    def draft_by_message(self, message_id):
        return self._first("drafts", {"telegram_message_id": f"eq.{message_id}", "select": "*"})

    def latest_pending(self):
        return self._first("drafts", {"status": "eq.pending", "order": "id.desc", "select": "*"})

    def decide(self, draft_id, status):
        self._req("PATCH", "drafts", {"id": f"eq.{draft_id}"}, {"status": status, "decided_at": now()},
                  "return=minimal")

    def note_scores(self):
        return [r["score"] for r in self._req("GET", "notes", {"select": "score"})]

    def draft_statuses(self):
        return [r["status"] for r in self._req("GET", "drafts", {"select": "status"})]

    def pending(self):
        return self._req("GET", "drafts", {"status": "eq.pending", "order": "id", "select": "id,note_text,created_at"})


def get_store():
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        return SupabaseStore(url, key)
    return SQLiteStore()
