"""WhatsApp Cloud API: sending messages, parsing webhooks and verifying signatures."""
from __future__ import annotations

import hashlib
import hmac
import logging

import requests

log = logging.getLogger(__name__)

# WhatsApp interactive-message limits (characters)
BUTTON_TITLE, ROW_TITLE, ROW_DESC, LIST_BUTTON, BODY = 20, 24, 72, 20, 1024


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------ outgoing message builders
# The conversation engine returns these plain dicts; a Sender turns them into API calls.

def text(body: str) -> dict:
    return {"kind": "text", "body": body}


def buttons(body: str, options: list[tuple[str, str]]) -> dict:
    """Up to 3 reply buttons: options = [(id, title)]."""
    return {"kind": "buttons", "body": body, "options": options[:3]}


def menu_list(body: str, button: str, rows: list[tuple[str, str, str]], section: str = "Opciones") -> dict:
    """Up to 10 list rows: rows = [(id, title, description)]."""
    return {"kind": "list", "body": body, "button": button, "rows": rows[:10], "section": section}


def to_api_payload(to: str, message: dict) -> dict:
    base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
    if message["kind"] == "text":
        return {**base, "type": "text", "text": {"body": clip(message["body"], 4096), "preview_url": True}}
    if message["kind"] == "buttons":
        return {**base, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": clip(message["body"], BODY)},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": clip(t, BUTTON_TITLE)}}
                                   for i, t in message["options"]]}}}
    if message["kind"] == "list":
        return {**base, "type": "interactive", "interactive": {
            "type": "list", "body": {"text": clip(message["body"], BODY)},
            "action": {"button": clip(message["button"], LIST_BUTTON), "sections": [{
                "title": clip(message["section"], ROW_TITLE),
                "rows": [{"id": i, "title": clip(t, ROW_TITLE), **({"description": clip(d, ROW_DESC)} if d else {})}
                         for i, t, d in message["rows"]]}]}}}
    if message["kind"] == "template":
        return {**base, "type": "template", "template": {
            "name": message["name"], "language": {"code": message["lang"]},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in message["params"]]}]}}
    raise ValueError(f"unknown message kind {message['kind']}")


class WhatsAppClient:
    def __init__(self, token: str, phone_number_id: str, graph_version: str = "v23.0",
                 session: requests.Session | None = None):
        self.url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
        self.token = token
        self.session = session or requests.Session()

    def send(self, to: str, message: dict) -> bool:
        ok, status = self._post(to, message)
        alt = alternate_number(to)
        if not ok and status == 400 and alt:
            # Argentina (549…) and Mexico (521…): webhooks include a mobile prefix that some accounts
            # (e.g. Meta's test numbers) reject when replying. Retry once without it.
            ok, _ = self._post(alt, message)
            if ok:
                log.info("Delivered to %s using the alternate format %s", to, alt)
        return ok

    def _post(self, to: str, message: dict) -> tuple[bool, int]:
        try:
            resp = self.session.post(self.url, json=to_api_payload(to, message), timeout=15,
                                     headers={"Authorization": f"Bearer {self.token}"})
            if resp.status_code >= 400:
                log.error("WhatsApp API %s: %s", resp.status_code, resp.text[:300])
                return False, resp.status_code
            return True, resp.status_code
        except requests.RequestException as exc:
            log.error("WhatsApp API unreachable: %s", exc)
            return False, 0


def alternate_number(number: str) -> str | None:
    """5492991234567 -> 542991234567 (Argentina), 5215512345678 -> 525512345678 (Mexico)."""
    if number.startswith("549") and len(number) == 13:
        return "54" + number[3:]
    if number.startswith("521") and len(number) == 13:
        return "52" + number[3:]
    return None


# ------------------------------------------------------------------ incoming webhooks

def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Checks X-Hub-Signature-256 so only Meta can call the webhook."""
    if not app_secret:
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_incoming(payload: dict) -> list[dict]:
    """Flattens a webhook payload into [{id, from, name, type: text|reply|other, text, reply_id}]."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                item = {"id": m.get("id"), "from": m.get("from"), "name": names.get(m.get("from"), ""),
                        "type": "other", "text": "", "reply_id": ""}
                if m.get("type") == "text":
                    item.update(type="text", text=m["text"].get("body", ""))
                elif m.get("type") == "interactive":
                    inter = m["interactive"]
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    item.update(type="reply", reply_id=reply.get("id", ""), text=reply.get("title", ""))
                elif m.get("type") == "button":  # quick-reply button on a template
                    item.update(type="reply", reply_id=m["button"].get("payload", ""), text=m["button"].get("text", ""))
                messages.append(item)
    return messages
