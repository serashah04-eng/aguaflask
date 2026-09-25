"""Vercel entrypoint: a plain WSGI app. Telegram POSTs each message to /api/webhook (set up with set_webhook.py).

It runs the same pipeline as local mode (bot.process), storing everything in Supabase.
"""

import hmac
import json
import os

import bot

_store = None


def store():
    global _store
    if _store is None:  # reused across requests while the function instance stays warm
        _store = bot.setup()
        if type(_store).__name__ != "SupabaseStore":
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set on Vercel")
    return _store


def app(environ, start_response):
    def reply(status, text):
        start_response(status, [("Content-Type", "text/plain; charset=utf-8")])
        return [text.encode()]

    method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
    if method == "GET":
        return reply("200 OK", "Skinstinct bot webhook is live. Telegram sends messages to /api/webhook by POST.")
    if method != "POST" or path.rstrip("/") != "/api/webhook":
        return reply("404 Not Found", "not found")

    # Only Telegram knows this secret (set with set_webhook.py), so nobody else can post fake notes.
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    received = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
    if not expected or not hmac.compare_digest(expected, received):
        return reply("401 Unauthorized", "unauthorised")

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        update = json.loads(environ["wsgi.input"].read(length))
        bot.process(store(), update)
    except Exception as e:  # always answer 200 so Telegram doesn't keep retrying a broken update
        print(f"webhook error: {type(e).__name__}: {e}")
    return reply("200 OK", "ok")
