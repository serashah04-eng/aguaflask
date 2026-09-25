"""Vercel entrypoint: a plain WSGI app. Telegram POSTs each message to /api/webhook (set up with set_webhook.py).

It runs the same pipeline as local mode (bot.process), storing everything in Supabase.
GET /api/health reports which settings are present and whether Supabase is reachable (never the values).
"""

import hmac
import json
import os
import traceback

import bot

REQUIRED = ["TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "TELEGRAM_WEBHOOK_SECRET", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]
_store = None


def store():
    global _store
    if _store is None:  # reused across requests while the function instance stays warm
        missing = [k for k in REQUIRED if not os.environ.get(k)]
        if missing:
            raise RuntimeError("Missing Vercel environment variables: " + ", ".join(missing))
        _store = bot.setup()
    return _store


def health():
    report = {"env": {k: bool(os.environ.get(k)) for k in REQUIRED + ["TELEGRAM_ALLOWED_USER_ID"]}}
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    report["supabase_url_looks_right"] = url.startswith("https://") and ".supabase.co" in url and "/rest/" not in url
    report["supabase_key_type"] = ("secret (sb_secret_...) - correct" if key.startswith("sb_secret_")
                                   else "legacy service_role JWT - correct" if key.startswith("eyJ")
                                   else "publishable/anon key - WRONG, use the secret key" if key.startswith("sb_publishable_")
                                   else "unrecognised" if key else "missing")
    try:
        s = store()
        for table in ("notes", "drafts", "voice_skill", "settings", "processed_updates"):
            s._req("GET", table, {"select": "*", "limit": "1"})
        report["supabase"] = "ok: all 5 tables reachable"
    except Exception as e:  # never echo the message: it can contain the URL or a key
        code = getattr(e, "code", "")
        report["supabase"] = f"FAILED: {type(e).__name__} {code}".strip()
    return report


def tell_chat(update, text):
    chat = (update.get("message") or {}).get("chat", {}).get("id")
    if chat:
        try:
            bot.send(chat, text)
        except Exception:
            traceback.print_exc()


def app(environ, start_response):
    def reply(status, text, content_type="text/plain; charset=utf-8"):
        start_response(status, [("Content-Type", content_type)])
        return [text.encode()]

    method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/").rstrip("/")
    if method == "GET" and path == "/api/health":
        return reply("200 OK", json.dumps(health(), indent=2), "application/json")
    if method == "GET":
        return reply("200 OK", "Skinstinct bot webhook is live. Telegram sends messages to /api/webhook by POST.")
    if method != "POST" or path != "/api/webhook":
        return reply("404 Not Found", "not found")

    # Only Telegram knows this secret (set with set_webhook.py), so nobody else can post fake notes.
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    received = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
    if not expected or not hmac.compare_digest(expected, received):
        return reply("401 Unauthorized", "unauthorised")

    update = {}
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        update = json.loads(environ["wsgi.input"].read(length))
        bot.process(store(), update)
    except Exception as e:  # answer 200 so Telegram doesn't retry forever, but say what broke
        traceback.print_exc()
        tell_chat(update, f"Setup problem on Vercel, so I couldn't process that: {type(e).__name__}: {str(e)[:300]}")
    return reply("200 OK", "ok")
