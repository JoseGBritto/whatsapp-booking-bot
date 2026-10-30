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
    app = Flask(__name__)

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
        if not verify_signature(settings.app_secret, raw, request.headers.get("X-Hub-Signature-256")):
            abort(401)
        for msg in parse_incoming(request.get_json(silent=True) or {}):
            if not msg["id"] or not assistant.db.first_time_seen(msg["id"]):
                continue  # duplicate delivery from Meta's retries
            try:
                for to, out in assistant.handle(msg):
                    client.send(to, out)
            except Exception:  # never return 500: Meta would keep retrying the same message
                log.exception("Failed to handle message %s", msg["id"])
        return jsonify(status="ok")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app
