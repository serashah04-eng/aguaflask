#!/usr/bin/env python3
"""Point Telegram at the Vercel deployment (or check the current setting).

  python set_webhook.py --make-secret                     create TELEGRAM_WEBHOOK_SECRET in .env (do this first)
  python set_webhook.py https://your-project.vercel.app    connect the bot to Vercel
  python set_webhook.py --status                          show where Telegram is sending messages

Copy TELEGRAM_WEBHOOK_SECRET from .env into Vercel's environment variables, so the
function can check that each request really comes from Telegram.
"""

import secrets
import sys

import drafter
from drafter import ROOT, telegram


def ensure_secret():
    env_file = ROOT / ".env"
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    for line in text.splitlines():
        if line.startswith("TELEGRAM_WEBHOOK_SECRET=") and line.split("=", 1)[1].strip():
            return line.split("=", 1)[1].strip(), False
    secret = secrets.token_urlsafe(32)
    lines = [l for l in text.splitlines() if not l.startswith("TELEGRAM_WEBHOOK_SECRET=")]
    lines.append(f"TELEGRAM_WEBHOOK_SECRET={secret}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return secret, True


def status():
    info = telegram("getWebhookInfo")
    print(f"Webhook URL:     {info.get('url') or '(none: bot runs locally with python bot.py)'}")
    print(f"Pending updates: {info.get('pending_update_count', 0)}")
    if info.get("last_error_message"):
        print(f"Last error:      {info['last_error_message']}")


def main():
    drafter.load_env()
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    if sys.argv[1] == "--status":
        status()
        return
    if sys.argv[1] == "--make-secret":
        _, created = ensure_secret()
        print("Created" if created else "Already have", "TELEGRAM_WEBHOOK_SECRET in .env. "
              "Open .env and copy its value into Vercel > Settings > Environment Variables.")
        return
    base = sys.argv[1].rstrip("/")
    if not base.startswith("https://"):
        sys.exit("Use the https:// URL of your Vercel deployment.")
    secret, created = ensure_secret()
    if created:
        print("Created TELEGRAM_WEBHOOK_SECRET in .env. Copy it into Vercel's environment variables, then redeploy.")
    telegram("setWebhook", url=f"{base}/api/webhook", secret_token=secret,
             allowed_updates='["message"]', max_connections=5)
    print(f"Telegram will now send messages to {base}/api/webhook")
    status()


if __name__ == "__main__":
    main()
