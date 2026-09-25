"""Vercel serverless function: Telegram POSTs each message here (set up with set_webhook.py).

It runs the same pipeline as local mode (bot.process), storing everything in Supabase.
"""

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parent.parent))  # project root: bot.py, drafter.py, storage.py

import bot  # noqa: E402

_store = None


def store():
    global _store
    if _store is None:  # reused across requests while the function instance stays warm
        _store = bot.setup()
        if type(_store).__name__ != "SupabaseStore":
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set on Vercel")
    return _store


class handler(BaseHTTPRequestHandler):
    def _reply(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        self._reply(200, "Skinstinct bot webhook is live. Telegram sends messages here by POST.")

    def do_POST(self):
        # Only Telegram knows this secret (set with set_webhook.py), so nobody else can post fake notes.
        expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        received = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected or not hmac.compare_digest(expected, received):
            self._reply(401, "unauthorised")
            return
        try:
            update = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            bot.process(store(), update)
        except Exception as e:  # always answer 200 so Telegram doesn't keep retrying a broken update
            print(f"webhook error: {type(e).__name__}: {e}")
        self._reply(200, "ok")
