"""Flask webhook for the WhatsApp Cloud API."""
from __future__ import annotations

import logging

from flask import Flask, abort, jsonify, request

from .business import Business
from .config import Settings
from .db import Database
from .flow import Assistant
from .whatsapp import WhatsAppClient, parse_incoming, verify_signature

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, assistant: Assistant | None = None, client=None) -> Flask:
    settings = settings or Settings.from_env()
    if assistant is None:
        assistant = Assistant(Business.load(settings.business_file), Database(settings.db_path), settings.owner_phone)
    client = client or WhatsAppClient(settings.whatsapp_token, settings.phone_number_id, settings.graph_version)
    if not settings.app_secret and not settings.allow_unsigned_webhooks:
        raise RuntimeError("Set WHATSAPP_APP_SECRET so webhook signatures can be verified "
                           "(or ALLOW_UNSIGNED_WEBHOOKS=1 for local testing only).")
    if not settings.app_secret:
        log.warning("ALLOW_UNSIGNED_WEBHOOKS=1: signatures are NOT verified. Never use this in production.")
    app = Flask(__name__)

    def deliver(to: str, message: dict) -> None:
        """Owner alerts go out as an approved template when one is configured: WhatsApp drops free-form
        messages to anyone who hasn't written to the number in the last 24 hours, the owner included."""
        if to == settings.owner_phone and settings.owner_alert_template and message["kind"] == "text":
            flat = " · ".join(line.strip() for line in message["body"].splitlines() if line.strip())
            message = {"kind": "template", "name": settings.owner_alert_template,
                       "lang": settings.owner_alert_template_lang, "params": [flat[:1000]]}
        client.send(to, message)

    @app.get("/webhook")
    def verify():
        """Meta calls this once when you register the webhook URL."""
        if (request.args.get("hub.mode") == "subscribe"
                and settings.verify_token and request.args.get("hub.verify_token") == settings.verify_token):
            return request.args.get("hub.challenge", ""), 200
        abort(403)

    @app.post("/webhook")
    def receive():
        raw = request.get_data()
        signed = verify_signature(settings.app_secret, raw, request.headers.get("X-Hub-Signature-256"))
        if not signed and not (settings.allow_unsigned_webhooks and not settings.app_secret):
            abort(401)
        for msg in parse_incoming(request.get_json(silent=True) or {}):
            if not msg["id"] or not assistant.db.first_time_seen(msg["id"]):
                continue  # duplicate delivery from Meta's retries
            try:
                for to, out in assistant.handle(msg):
                    deliver(to, out)
            except Exception:  # never return 500: Meta would keep retrying the same message
                log.exception("Failed to handle message %s", msg["id"])
        return jsonify(status="ok")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app
