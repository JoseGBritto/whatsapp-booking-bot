"""CLI:  python -m booking serve | simulate | remind"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime

from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m booking", description="WhatsApp booking assistant")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the webhook server")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("simulate", help="chat with the assistant in the terminal (no WhatsApp needed)")
    sub.add_parser("remind", help="send reminders for appointments in the next 24 h (run hourly)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()

    if args.cmd == "serve":
        from .server import create_app
        create_app(settings).run(host="0.0.0.0", port=args.port)
    elif args.cmd == "simulate":
        from .simulate import main as simulate
        simulate(settings.business_file)
    else:
        from .business import Business
        from .db import Database
        from .reminders import send_reminders
        from .whatsapp import WhatsAppClient
        business = Business.load(settings.business_file)
        client = WhatsAppClient(settings.whatsapp_token, settings.phone_number_id, settings.graph_version)
        sent = send_reminders(business, Database(settings.db_path), client, datetime.now(business.tz),
                              settings.reminder_template, settings.reminder_template_lang)
        print(f"Reminders sent: {sent}")


if __name__ == "__main__":
    main()
